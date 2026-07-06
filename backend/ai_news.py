"""AI 新聞摘要 agent（pipeline 式、非自主瀏覽——控 token 成本的關鍵設計）。

流程：
  ① 抓取（免費零 token）：官方 blog RSS（OpenAI/Google/HuggingFace）+ 科技媒體 AI 版
     （TechCrunch/The Verge）+ HackerNews 首頁（Algolia API）
  ② 本地去重（seen store）+ 關鍵字預過濾（HN 才需要；其他源本身就是 AI 專版）
  ③ LLM（Gemini 免費層）整合簡報：讀全部標題 + google_search 補社群/Fed 發言 →
     產業分類條列（四級可信度標籤）+ 趨勢情境機率
     — 搜尋失敗退純文字分析；無 GEMINI_API_KEY / 簡報失敗再降純標題版
  ④ 經 notifier 發 Telegram（含重試；簡報過長時分多則附頁碼）

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

# RSS 來源；單源壞掉不影響其他（鉅亨網 RSS 陣亡的教訓）
# AI 專版源不需再過濾；Fed 官方源供「總經與 Fed」段（2026-07-05 簡報版加入）
RSS_SOURCES = [
    {"name": "OpenAI", "url": "https://openai.com/news/rss.xml"},
    {"name": "Google AI", "url": "https://blog.google/technology/ai/rss/"},
    {"name": "HuggingFace", "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "TechCrunch", "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
    {"name": "Fed 新聞稿", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "Fed 演說", "url": "https://www.federalreserve.gov/feeds/speeches.xml"},
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

def _parse_rss(raw: bytes | str, source: str) -> list[dict]:
    out = []
    try:
        # 必須餵 bytes：Fed feed 是 text/xml 無 charset，requests.text 會用 ISO-8859-1
        # 把 UTF-8 BOM/非 ASCII 解成亂碼讓解析炸掉（mini 實跑抓到）；bytes 讓 expat 自行判編碼
        root = ET.fromstring(raw)
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
            got = _parse_rss(r.content, src["name"])
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


_LABEL_ICON = {"已證實": "✅", "報導": "📰", "傳言": "❓", "臆測": "💭"}
_VALID_LABELS = tuple(_LABEL_ICON)   # 白名單與 icon 同源，新增層級只改一處


def _gemini_generate(prompt: str, use_search: bool, timeout: int = 90) -> str | None:
    """單次 Gemini 呼叫、回純文字。key 走 header 不進 URL/log。"""
    key, model = _gemini_config()
    if not key:
        return None
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3}}
    if use_search:
        body["tools"] = [{"google_search": {}}]
    try:
        r = requests.post(
            GEMINI_URL.format(model=model),
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json=body, timeout=timeout,
        )
        if r.status_code != 200:
            logger.warning("[ai_news] Gemini HTTP %d (search=%s): %s",
                           r.status_code, use_search, r.text[:150])
            return None
        cand = r.json()["candidates"][0]
        return "".join(p.get("text", "") for p in cand["content"]["parts"])
    except Exception as e:
        logger.warning("[ai_news] Gemini 失敗 (search=%s): %s", use_search, str(e)[:150])
        return None


def _clamp_label(label) -> str:
    """可信度標籤白名單：非四級一律降為臆測（保守）。"""
    s = str(label or "").strip()
    return s if s in _VALID_LABELS else "臆測"


def _safe_pct(v) -> int:
    """probability_pct 容錯轉 int（"40%"、"約40"、null 都不炸），夾在 0-100。

    取第一個數字 token（"40-60%" 取 40）；全串黏數字（4060→100）會嚴重失真。
    """
    try:
        m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
        return max(0, min(100, int(float(m.group(0))))) if m else 0
    except Exception:
        return 0


def _parse_briefing(text: str, grounded: bool) -> dict | None:
    """Gemini 回覆 → 清洗後簡報 dict；解析失敗或清洗後全空回 None（讓上層重試/降級）。"""
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        b = json.loads(m.group(0)) if m else {}
        # 清洗 + 上限（防爆長）；label 白名單
        out = {"grounded": grounded, "sections": [], "social": [], "fed": [],
               "outlook": None}
        for sec in (b.get("sections") or [])[:6]:
            rows = [{"label": _clamp_label(x.get("label")),
                     "text": str(x.get("text", ""))[:220]}
                    for x in (sec.get("items") or [])[:8] if x.get("text")]
            if rows:
                out["sections"].append({"sector": str(sec.get("sector", "其他"))[:20],
                                        "items": rows})
        for x in (b.get("social") or [])[:6]:
            if x.get("text"):
                out["social"].append({"who": str(x.get("who", "?"))[:40],
                                      "label": _clamp_label(x.get("label")),
                                      "text": str(x.get("text", ""))[:220]})
        for x in (b.get("fed") or [])[:4]:
            if x.get("text"):
                out["fed"].append({"label": _clamp_label(x.get("label")),
                                   "text": str(x.get("text", ""))[:220]})
        o = b.get("outlook") or {}
        scen = [{"name": str(s.get("name", ""))[:60],
                 "probability_pct": _safe_pct(s.get("probability_pct")),
                 "rationale": str(s.get("rationale", ""))[:160]}
                for s in (o.get("scenarios") or [])[:4] if s.get("name")]
        if scen:
            out["outlook"] = {"horizon": str(o.get("horizon", "1-3個月"))[:20],
                              "scenarios": scen}
        # 清洗後三段全空 → 視為失敗，別發只剩標題列的空殼訊息
        if not (out["sections"] or out["social"] or out["fed"]):
            return None
        return out
    except Exception as e:
        logger.warning("[ai_news] 簡報解析失敗: %s", str(e)[:150])
        return None


def briefing_with_gemini(items: list[dict]) -> dict | None:
    """整合簡報：讀完全部標題 + google 搜尋補社群/Fed 發言 → 結構化 JSON。

    回 {sections, social, fed, outlook, grounded}；無 key / 全失敗回 None。
    可信度四級判準（用戶要求「精準看出新聞用詞」）：
      已證實=官方公告/當事人親口；報導=具名媒體但未官方確認；
      傳言=匿名消息/rumor；臆測=分析師預測/評論。
    """
    if not items:
        return None
    # Fed 源保留名額：照抓取順序切 [:80] 時 Fed（排在 5 個 AI 源之後）會先被切掉，
    # 且 filter_fresh 已標 seen → 永久遺失（review 抓到）
    fed = [it for it in items if it["source"].startswith("Fed")][:20]
    rest = [it for it in items if not it["source"].startswith("Fed")][:80 - len(fed)]
    numbered = "\n".join(f"{i}. [{it['source']}] {it['title']}"
                         for i, it in enumerate(fed + rest))
    prompt = (
        "你是財經科技情報分析師，任務是把資訊整理成「看得懂」的繁體中文簡報。\n"
        "以下是最新新聞標題清單（含 AI 官方 blog、科技媒體、HackerNews、Fed 官方 RSS）：\n\n"
        f"{numbered}\n\n"
        "請執行：\n"
        "1. 用 Google 搜尋補充過去 24 小時內：(a) 美國總統的社群發言/重大表態 "
        "(b) 主要科技公司領袖（Musk、Altman、黃仁勳、Zuckerberg、Pichai 等）的社群發言或公開表態 "
        "(c) Fed 官員最新發言與市場解讀。\n"
        "2. 對每條資訊做可信度判級，只能用四級：已證實（官方公告/當事人親口）、"
        "報導（具名媒體報導但未官方確認）、傳言（匿名消息來源/rumored）、"
        "臆測（分析師預測/評論/could/may 類用詞）。判級依原文用詞線索。\n"
        "3. 依產業分類條列（每條一句話：發生什麼＋為何重要）。分類建議：AI 模型與應用／"
        "晶片與半導體／科技大廠動態／政策與監管／其他。無內容的分類省略。\n"
        "4. 給未來 1-3 個月趨勢推斷：2-4 個情境、各附主觀機率%（總和 100）與一句理由。\n"
        "只回 JSON 物件（不要 markdown 包裹）：\n"
        '{"sections": [{"sector": "分類名", "items": [{"label": "已證實|報導|傳言|臆測", "text": "一句話"}]}], '
        '"social": [{"who": "人物(職銜)", "label": "同上四級", "text": "說了什麼+含義"}], '
        '"fed": [{"label": "同上", "text": "發言+市場解讀"}], '
        '"outlook": {"horizon": "1-3個月", "scenarios": [{"name": "情境", "probability_pct": 40, "rationale": "理由"}]}}'
    )
    # parse 放在重試圈內：搜尋版「連線失敗或回文解析不出」都退純文字版（review 抓到
    # 原本只在 transport 失敗時 fallback，grounding 回 refusal/非 JSON 就整個放棄）
    briefing = None
    text = _gemini_generate(prompt, use_search=True)
    if text is not None:
        briefing = _parse_briefing(text, grounded=True)
    if briefing is None:
        text = _gemini_generate(
            prompt + "\n（註：目前無法使用 Google 搜尋。social/fed 只能取材自上方標題清單，"
            "沒有對應標題就回空陣列；嚴禁憑既有知識或記憶編造任何人的發言。）",
            use_search=False)
        if text is not None:
            briefing = _parse_briefing(text, grounded=False)
    return briefing


# ── ④ 組訊息 + 發送 ─────────────────────────────────────

TG_MSG_LIMIT = 3900  # Telegram 上限 4096、留 buffer；超過就少放幾則


def _esc(s: str) -> str:
    # quote=True 必要：值會進 href="..." 屬性，URL 含 " 會破壞 HTML → Telegram 拒收整則
    return html.escape(str(s or ""), quote=True)


def build_digest(items: list[dict]) -> str:
    """純標題版（無 key / 簡報失敗時的 fallback）。"""
    lines = ["🤖 <b>AI 快訊摘要</b>", "（未設 GEMINI_API_KEY 或簡報失敗 → 純標題版）"]
    total = sum(len(x) + 1 for x in lines)
    for i, it in enumerate(items[:DIGEST_TOP_N], 1):
        line = (f"{i}. <a href=\"{_esc(it['link'])}\">{_esc(it['title'])}</a>"
                f"（{_esc(it['source'])}）")
        if total + len(line) + 1 > TG_MSG_LIMIT:  # 超長就停在這、避免 Telegram 4096 拒收
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _split_oversized(st: str) -> list[str]:
    """單一段落字串就超過上限時按行再切（行間切不會截斷 HTML tag / entity）。

    escape 後單段可膨脹超過 Telegram 4096（220 字上限是 escape 前算的）→ 先切再裝箱。
    """
    if len(st) <= TG_MSG_LIMIT:
        return [st]
    parts, cur = [], ""
    for ln in st.split("\n"):
        if cur and len(cur) + len(ln) + 1 > TG_MSG_LIMIT:
            parts.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        parts.append(cur)
    return parts


def format_briefing(b: dict) -> list[str]:
    """簡報 dict → Telegram HTML 訊息串（依段落切塊、每塊 ≤ TG_MSG_LIMIT）。"""
    blocks: list[str] = []
    head = "🤖 <b>AI 情報簡報</b>"
    if not b.get("grounded"):
        head += "\n（本次無法即時搜尋、社群/Fed 段可能不完整）"

    sec_txt = []
    for sec in b.get("sections", []):
        rows = [f"• {_LABEL_ICON.get(it['label'], '💭')}【{_esc(it['label'])}】{_esc(it['text'])}"
                for it in sec["items"]]
        sec_txt.append(f"━ <b>{_esc(sec['sector'])}</b>\n" + "\n".join(rows))

    if b.get("social"):
        rows = [f"• {_esc(x['who'])} {_LABEL_ICON.get(x['label'], '💭')}【{_esc(x['label'])}】{_esc(x['text'])}"
                for x in b["social"]]
        sec_txt.append("━ <b>💬 政要與科技領袖社群</b>\n" + "\n".join(rows))
    if b.get("fed"):
        rows = [f"• {_LABEL_ICON.get(x['label'], '💭')}【{_esc(x['label'])}】{_esc(x['text'])}"
                for x in b["fed"]]
        sec_txt.append("━ <b>🏦 Fed 動態</b>\n" + "\n".join(rows))
    if b.get("outlook"):
        o = b["outlook"]
        rows = [f"• {_esc(s['name'])} <b>{s['probability_pct']}%</b>：{_esc(s['rationale'])}"
                for s in o["scenarios"]]
        sec_txt.append(f"━ <b>🔮 趨勢推斷（{_esc(o['horizon'])}）</b>\n" + "\n".join(rows)
                       + "\n<i>※ 機率為模型主觀推測、僅供參考、非投資建議</i>")

    # 依段落裝箱：塞得下就同一則、塞不下開新則；單段超長先按行切
    cur = head
    for st in sec_txt:
        for seg in _split_oversized(st):
            if len(cur) + len(seg) + 2 > TG_MSG_LIMIT:
                blocks.append(cur)
                cur = seg
            else:
                cur += "\n\n" + seg
    blocks.append(cur)
    if len(blocks) > 1:  # 多則時加頁碼
        blocks = [f"{blk}\n<i>({i}/{len(blocks)})</i>" for i, blk in enumerate(blocks, 1)]
    return blocks


def run_digest() -> dict:
    """主入口：抓 → 去重 → Gemini 整合簡報（含搜尋社群/Fed）→ Telegram（可多則）。"""
    items = fetch_all_items()
    fresh = filter_fresh(items)
    if not fresh:
        logger.info("[ai_news] 無新項目、本次不發送")
        return {"fetched": len(items), "fresh": 0, "briefing": False,
                "grounded": False, "messages": 0, "sent": False}
    briefing = briefing_with_gemini(fresh)
    from notifier import send_telegram
    msgs = format_briefing(briefing) if briefing else [build_digest(fresh)]
    ok_all = True
    for m in msgs:
        if not send_telegram(m):  # 單則失敗即停：Telegram 掛掉時別每頁重燒整組重試
            ok_all = False
            break
    has_briefing = briefing is not None
    grounded = bool(briefing and briefing.get("grounded"))
    logger.info("[ai_news] fetched=%d fresh=%d briefing=%s grounded=%s msgs=%d sent=%s",
                len(items), len(fresh), has_briefing, grounded, len(msgs), ok_all)
    return {"fetched": len(items), "fresh": len(fresh), "briefing": has_briefing,
            "grounded": grounded, "messages": len(msgs), "sent": ok_all}
