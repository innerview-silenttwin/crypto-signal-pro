"""AI 新聞摘要 agent（pipeline 式、非自主瀏覽——控 token 成本的關鍵設計）。

流程：
  ① 抓取（免費零 token）：官方 blog RSS（OpenAI/Google/HuggingFace）+ 科技媒體 AI 版
     （TechCrunch/The Verge）+ HackerNews 首頁（Algolia API）
  ② 本地去重（seen store）+ 關鍵字預過濾（HN 才需要；其他源本身就是 AI 專版）
  ③ LLM（Gemini 免費層）只吃「標題+來源」批次：挑重點 + 一行繁中摘要
     — 無 GEMINI_API_KEY 時自動降級為純標題版（仍可用）
  ④ 經 notifier 發 Telegram（含重試）

安全：
  - GEMINI_API_KEY 走 .env、用 `x-goog-api-key` header（不放 URL → 例外訊息不會帶 key）
  - 本模組只處理公開新聞，絕不接觸交易/帳戶資料（免費層資料可能被 Google 用於改進產品）
  - 標題進 Telegram HTML 前一律 escape
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
SEEN_PATH = _BASE_DIR / "data" / "ai_news_seen.json"
SEEN_TTL_DAYS = 14

# RSS 來源（皆 AI 專版 → 不需再過濾）；單源壞掉不影響其他（鉅亨網 RSS 陣亡的教訓）
RSS_SOURCES = [
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml"},
    {"name": "Google AI", "url": "https://blog.google/technology/ai/rss/"},
    {"name": "HuggingFace", "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
]
_HN_API = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"

# HN 首頁抓回來是全主題 → 用關鍵字挑 AI 相關（不分大小寫；可自行增修）
# 全部加字界 \b —— 否則 "ai" 會誤匹配 chair/email/Airbnb（review 實證抓到）
AI_KEYWORDS = [
    "ai", "llm", "llms", "gpt", "claude", "gemini", "openai", "anthropic", "deepmind",
    "machine learning", "deep learning", "neural", "transformer", "diffusion",
    "agent", "agents", "agentic", "rag", "fine-tune", "fine-tuning", "finetune",
    "inference", "chatbot", "copilot", "mistral",
    "llama", "qwen", "deepseek", "hugging face", "nvidia", "tpu", "cuda",
]
_KW_RE = re.compile(
    "|".join(rf"\b{re.escape(k)}\b" for k in AI_KEYWORDS), re.IGNORECASE)

_UA = {"User-Agent": "Mozilla/5.0 (csp-ai-news)"}
MAX_PER_SOURCE = 15      # 每源最多取 N 則（RSS 大 feed 只看最新）
DIGEST_TOP_N = 10        # 摘要最多幾則


# ── ① 抓取 ──────────────────────────────────────────────

def _parse_rss(text: str, source: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        logger.warning("[ai_news] %s RSS 解析失敗: %s", source, e)
        return out
    items = root.findall(".//item")
    if not items:  # Atom
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall(".//a:entry", ns)[:MAX_PER_SOURCE]:
            title = (e.findtext("a:title", "", ns) or "").strip()
            link_el = e.find("a:link", ns)
            link = (link_el.get("href") if link_el is not None else "") or ""
            if title:
                out.append({"title": title, "link": link.strip(), "source": source})
        return out
    for it in items[:MAX_PER_SOURCE]:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if title:
            out.append({"title": title, "link": link, "source": source})
    return out


def fetch_all_items() -> list[dict]:
    """抓所有來源。單源失敗記 log 續跑。"""
    items: list[dict] = []
    for src in RSS_SOURCES:
        try:
            r = requests.get(src["url"], timeout=12, headers=_UA)
            if r.status_code != 200:
                logger.warning("[ai_news] %s HTTP %d", src["name"], r.status_code)
                continue
            got = _parse_rss(r.text, src["name"])
            items.extend(got)
            logger.info("[ai_news] %s 取得 %d 則", src["name"], len(got))
        except Exception as e:
            logger.warning("[ai_news] %s 抓取失敗: %s", src["name"], e)
    # HackerNews 首頁（全主題 → 關鍵字過濾）
    try:
        r = requests.get(_HN_API, timeout=12, headers=_UA)
        hits = (r.json().get("hits") or []) if r.status_code == 200 else []
        n = 0
        for h in hits:
            title = (h.get("title") or "").strip()
            if not title or not _KW_RE.search(title):
                continue
            link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID', '')}"
            items.append({"title": title, "link": link, "source": "HackerNews"})
            n += 1
        logger.info("[ai_news] HackerNews 首頁 %d 則、AI 相關 %d 則", len(hits), n)
    except Exception as e:
        logger.warning("[ai_news] HackerNews 抓取失敗: %s", e)
    return items


# ── ② 去重 ──────────────────────────────────────────────

def _load_seen() -> dict:
    try:
        if SEEN_PATH.exists():
            return json.loads(SEEN_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_seen(seen: dict):
    try:
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False))
    except Exception as e:
        logger.warning("[ai_news] seen store 寫入失敗: %s", e)


def filter_fresh(items: list[dict], now: float | None = None) -> list[dict]:
    """去掉看過的（以 link 為 key，無 link 用標題）；同時把新項記入 store、清過期。"""
    now = now or time.time()
    seen = _load_seen()
    cutoff = now - SEEN_TTL_DAYS * 86400
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    fresh, dup_in_batch = [], set()
    for it in items:
        key = it.get("link") or it.get("title") or ""
        if not key or key in seen or key in dup_in_batch:
            continue
        dup_in_batch.add(key)
        seen[key] = now
        fresh.append(it)
    _save_seen(seen)
    return fresh


# ── ③ Gemini 摘要（可缺席降級）──────────────────────────

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _gemini_config() -> tuple:
    return (os.environ.get("GEMINI_API_KEY", "").strip(),
            os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip())


def summarize_with_gemini(items: list[dict]) -> list[dict] | None:
    """批次送「標題+來源」給 Gemini：挑 Top N + 一行繁中摘要。

    回 [{title, link, source, zh}]；無 key / 呼叫失敗回 None（呼叫端降級純標題版）。
    只送公開新聞標題、key 放 header 不放 URL。
    """
    key, model = _gemini_config()
    if not key or not items:
        return None
    numbered = "\n".join(f"{i}. [{it['source']}] {it['title']}" for i, it in enumerate(items))
    prompt = (
        "你是 AI 產業新聞編輯。以下是最新 AI 相關新聞標題清單，"
        f"請挑出最重要、對 AI 從業者最有價值的至多 {DIGEST_TOP_N} 則，"
        "依重要性排序，每則用一行繁體中文濃縮重點（不是翻譯標題、要講出為什麼重要）。\n"
        "只回 JSON array，格式：[{\"idx\": <清單編號>, \"zh\": \"一行中文重點\"}]\n\n"
        + numbered
    )
    try:
        r = requests.post(
            GEMINI_URL.format(model=model),
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.2}},
            timeout=40,
        )
        if r.status_code != 200:
            logger.warning("[ai_news] Gemini HTTP %d: %s", r.status_code, r.text[:200])
            return None
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        m = re.search(r"\[.*\]", text, re.DOTALL)  # 容忍 ```json 包裹
        picked = json.loads(m.group(0)) if m else []
        out = []
        for p in picked[:DIGEST_TOP_N]:
            idx = p.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(items):
                out.append({**items[idx], "zh": str(p.get("zh", "")).strip()})
        return out or None
    except Exception as e:
        # key 在 header、例外訊息不會帶 key；仍防禦性截斷
        logger.warning("[ai_news] Gemini 摘要失敗: %s", str(e)[:200])
        return None


# ── ④ 組訊息 + 發送 ─────────────────────────────────────

TG_MSG_LIMIT = 3900  # Telegram 上限 4096、留 buffer；超過就少放幾則


def _esc(s: str) -> str:
    # quote=True 必要：值會進 href="..." 屬性，URL 含 " 會破壞 HTML → Telegram 拒收整則
    return html.escape(str(s or ""), quote=True)


def build_digest(items: list[dict], summarized: list[dict] | None) -> str:
    lines = ["🤖 <b>AI 快訊摘要</b>"]
    if summarized:
        rows = summarized
    else:
        lines.append("（未設 GEMINI_API_KEY 或摘要失敗 → 純標題版）")
        rows = items[:DIGEST_TOP_N]
    total = sum(len(x) + 1 for x in lines)
    for i, it in enumerate(rows, 1):
        zh = f"\n    {_esc(it['zh'])}" if it.get("zh") else ""
        line = (f"{i}. <a href=\"{_esc(it['link'])}\">{_esc(it['title'])}</a>"
                f"（{_esc(it['source'])}）{zh}")
        if total + len(line) + 1 > TG_MSG_LIMIT:  # 超長就停在這、避免 Telegram 4096 拒收
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def run_digest() -> dict:
    """主入口：抓 → 去重 → (Gemini) → Telegram。回統計 dict。"""
    items = fetch_all_items()
    fresh = filter_fresh(items)
    if not fresh:
        logger.info("[ai_news] 無新項目、本次不發送")
        return {"fetched": len(items), "fresh": 0, "sent": False}
    summarized = summarize_with_gemini(fresh)
    msg = build_digest(fresh, summarized)
    from notifier import send_telegram
    ok = send_telegram(msg)
    logger.info("[ai_news] fetched=%d fresh=%d gemini=%s sent=%s",
                len(items), len(fresh), summarized is not None, ok)
    return {"fetched": len(items), "fresh": len(fresh),
            "gemini": summarized is not None, "sent": bool(ok)}
