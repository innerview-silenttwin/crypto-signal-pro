"""
消息面情緒分析層 (Sentiment Analysis Layer) — Phase 3

用 RSS 抓取台灣財經新聞，進行關鍵字情緒分析：
1. 抓取多個 RSS 來源的最新新聞
2. 比對股票代碼 / 公司名稱，篩選相關新聞
3. 根據正負面關鍵字計算情緒分數
4. 情緒偏多加分、偏空減分

資料來源：鉅亨網、Yahoo 股市、MoneyDJ 等公開 RSS
"""

import re
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import pandas as pd
import requests

from .base import BaseLayer, LayerModifier, LayerRegistry

logger = logging.getLogger(__name__)


# ── RSS 來源設定 ──

RSS_FEEDS = [
    {
        "name": "鉅亨網-台股",
        "url": "https://news.cnyes.com/news/cat/tw_stock/rss",
        "category": "tw_stock",
    },
    {
        "name": "鉅亨網-產業",
        "url": "https://news.cnyes.com/news/cat/industry/rss",
        "category": "industry",
    },
    {
        "name": "Yahoo股市新聞",
        "url": "https://tw.stock.yahoo.com/rss?category=tw-market",
        "category": "tw_market",
    },
]

# ── 新聞快取 ──

_news_cache: Dict = {}  # {"articles": [...], "time": float}
NEWS_CACHE_TTL = 1800   # 30 分鐘


# ── 情緒關鍵字辭典 ──

# 正面關鍵字（做多視角）
POSITIVE_KEYWORDS = {
    # 強正面 (權重 3)
    3: [
        "創新高", "突破", "大漲", "漲停", "暴漲", "飆漲", "強勢",
        "利多", "營收創高", "獲利創高", "大單", "法人買超", "外資買超",
        "上調目標價", "調升評等", "超預期", "優於預期", "需求強勁",
        "訂單滿載", "產能滿載", "供不應求", "毛利率提升",
        "股利增加", "配息增加", "回購", "庫藏股",
    ],
    # 中正面 (權重 2)
    2: [
        "看好", "看多", "利好", "反彈", "回升", "轉強", "上漲",
        "營收成長", "獲利成長", "業績增長", "表現亮眼", "表現優異",
        "投資", "擴廠", "新產品", "新技術", "合作", "結盟",
        "市佔率提升", "出貨增加", "接單",
    ],
    # 弱正面 (權重 1)
    1: [
        "穩健", "持穩", "正面", "樂觀", "回溫", "好轉", "改善",
        "持平", "符合預期", "維持", "支撐",
    ],
}

NEGATIVE_KEYWORDS = {
    # 強負面 (權重 -3)
    -3: [
        "崩盤", "暴跌", "重挫", "跌停", "大跌", "慘跌",
        "利空", "營收衰退", "獲利衰退", "虧損", "下修目標價",
        "調降評等", "低於預期", "不如預期", "需求疲弱",
        "砍單", "訂單減少", "庫存過高", "毛利率下滑",
        "裁員", "減資", "下市", "違約", "掏空",
    ],
    # 中負面 (權重 -2)
    -2: [
        "看空", "看淡", "利空", "下跌", "走弱", "轉弱", "衰退",
        "營收下滑", "獲利下降", "業績衰退", "表現不佳",
        "法人賣超", "外資賣超", "產能過剩", "價格下跌",
        "競爭加劇", "市佔率下滑",
    ],
    # 弱負面 (權重 -1)
    -1: [
        "疑慮", "風險", "不確定", "壓力", "挑戰", "隱憂",
        "觀望", "保守", "趨緩", "放緩",
    ],
}


# ── 股票名稱對照（用於新聞比對）──

def _strip_tw(symbol: str) -> str:
    """2330.TW → 2330"""
    return symbol.replace(".TW", "").replace(".TWO", "")


def _strip_html(text: str) -> str:
    """移除 HTML 標籤，只保留純文字"""
    return re.sub(r'<[^>]+>', '', text).strip()


@dataclass
class NewsArticle:
    """新聞文章"""
    title: str
    description: str  # 摘要/描述（RSS <description>）
    source: str
    published: str
    link: str
    category: str
    sentiment_score: float = 0.0
    matched_keywords: List[str] = field(default_factory=list)


# ── RSS 抓取 ──

def fetch_rss_articles() -> List[NewsArticle]:
    """
    從多個 RSS 來源抓取新聞

    Returns:
        NewsArticle 列表（最新的在前）
    """
    now = time.time()
    if _news_cache and now - _news_cache.get("time", 0) < NEWS_CACHE_TTL:
        return _news_cache["articles"]

    articles = []

    for feed in RSS_FEEDS:
        try:
            try:
                resp = requests.get(feed["url"], timeout=10, headers={
                    "User-Agent": "Mozilla/5.0 (CryptoSignalPro News Reader)",
                })
            except requests.exceptions.SSLError:
                resp = requests.get(feed["url"], timeout=10, verify=False, headers={
                    "User-Agent": "Mozilla/5.0 (CryptoSignalPro News Reader)",
                })

            if resp.status_code != 200:
                logger.warning(f"RSS 抓取失敗 {feed['name']}: HTTP {resp.status_code}")
                continue

            root = ET.fromstring(resp.text)

            # 支援 RSS 2.0 和 Atom 格式
            items = root.findall('.//item')
            if not items:
                # Atom 格式
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                items = root.findall('.//atom:entry', ns)

            for item in items[:20]:  # 每來源最多 20 則
                title = ""
                description = ""
                link = ""
                pub_date = ""

                # RSS 2.0
                title_el = item.find('title')
                if title_el is not None and title_el.text:
                    title = title_el.text.strip()

                desc_el = item.find('description')
                if desc_el is not None and desc_el.text:
                    description = _strip_html(desc_el.text.strip())

                link_el = item.find('link')
                if link_el is not None:
                    link = (link_el.text or link_el.get('href', '')).strip()

                pub_el = item.find('pubDate')
                if pub_el is not None and pub_el.text:
                    pub_date = pub_el.text.strip()

                # Atom fallback
                if not title:
                    title_el = item.find('{http://www.w3.org/2005/Atom}title')
                    if title_el is not None and title_el.text:
                        title = title_el.text.strip()

                if not description:
                    desc_el = item.find('{http://www.w3.org/2005/Atom}summary')
                    if desc_el is not None and desc_el.text:
                        description = _strip_html(desc_el.text.strip())

                if not link:
                    link_el = item.find('{http://www.w3.org/2005/Atom}link')
                    if link_el is not None:
                        link = link_el.get('href', '')

                if title:
                    articles.append(NewsArticle(
                        title=title,
                        description=description[:300],  # 限制長度
                        source=feed["name"],
                        published=pub_date,
                        link=link,
                        category=feed["category"],
                    ))

            logger.info(f"RSS [{feed['name']}] 抓取成功: {len(items[:20])} 則")

        except Exception as e:
            logger.warning(f"RSS 抓取異常 {feed['name']}: {e}")
            continue

    if articles:
        _news_cache["articles"] = articles
        _news_cache["time"] = now
        logger.info(f"新聞快取已更新: 共 {len(articles)} 則")

    return articles


# ── 情緒分析 ──

def analyze_sentiment(title: str, description: str = "") -> Tuple[float, List[str]]:
    """
    分析新聞的情緒分數（標題 + 摘要）

    標題容易「標題殺人」，因此大幅降低權重 (0.3x)。
    摘要內容較客觀完整，權重最高 (1.0x)，主導情緒判斷。
    若同一關鍵字在標題和摘要都出現，只計一次（以摘要為主）。

    Args:
        title: 新聞標題
        description: 新聞摘要/描述

    Returns:
        (sentiment_score, matched_keywords)
        score 範圍大致 -10 ~ +10
    """
    TITLE_WEIGHT = 0.3   # 標題權重（大幅降低，防標題殺人）
    DESC_WEIGHT = 1.0    # 摘要權重（主導判斷）

    score = 0.0
    matched = []
    seen_keywords = set()

    # 先分析摘要（權重高），再分析標題（權重低、去重）
    for text, text_weight, label in [
        (description, DESC_WEIGHT, "摘"),
        (title, TITLE_WEIGHT, "標"),
    ]:
        if not text:
            continue

        for kw_weight, keywords in POSITIVE_KEYWORDS.items():
            for kw in keywords:
                if kw in text and kw not in seen_keywords:
                    score += kw_weight * text_weight
                    matched.append(f"+{kw}({label})")
                    seen_keywords.add(kw)

        for kw_weight, keywords in NEGATIVE_KEYWORDS.items():
            for kw in keywords:
                if kw in text and kw not in seen_keywords:
                    score += kw_weight * text_weight
                    matched.append(f"-{kw}({label})")
                    seen_keywords.add(kw)

    return score, matched


def get_stock_sentiment(symbol: str, stock_name: str = "",
                        articles: Optional[List[NewsArticle]] = None) -> dict:
    """
    取得特定股票的消息面情緒

    Args:
        symbol: 股票代碼 (e.g. "2330.TW")
        stock_name: 股票名稱 (e.g. "台積電")
        articles: 新聞列表（若為 None 則自動抓取）

    Returns:
        {
            "score": float,         # 綜合情緒分數 (-100~+100 正規化)
            "positive_count": int,
            "negative_count": int,
            "neutral_count": int,
            "total_related": int,
            "recent_news": [...],   # 相關新聞列表
            "sentiment_label": str, # "偏多" / "中性" / "偏空"
            "advice": str,          # 做多建議文字
        }
    """
    if articles is None:
        articles = fetch_rss_articles()

    code = _strip_tw(symbol)

    # 搜尋關鍵字（代碼 + 名稱）
    search_terms = [code]
    if stock_name:
        search_terms.append(stock_name)

    # 比對相關新聞
    related = []
    for art in articles:
        is_related = False
        for term in search_terms:
            if term and term in art.title:
                is_related = True
                break

        if is_related:
            score, keywords = analyze_sentiment(art.title, art.description)
            art.sentiment_score = score
            art.matched_keywords = keywords
            related.append(art)

    # 統計
    pos_count = sum(1 for a in related if a.sentiment_score > 0)
    neg_count = sum(1 for a in related if a.sentiment_score < 0)
    neu_count = sum(1 for a in related if a.sentiment_score == 0)

    # 綜合分數（加權平均，越新的權重越高）
    if related:
        total_score = sum(a.sentiment_score for a in related)
        # 正規化到 -100 ~ +100
        raw = total_score / max(len(related), 1)
        normalized = max(-100, min(100, raw * 15))  # 放大倍率
    else:
        normalized = 0.0

    # 情緒標籤（做多視角）
    if normalized >= 20:
        label = "偏多"
        advice = "近期消息面偏正面，有利做多"
    elif normalized >= 5:
        label = "略偏多"
        advice = "消息面略正面，可正常操作"
    elif normalized <= -20:
        label = "偏空"
        advice = "近期消息面偏負面，建議觀望"
    elif normalized <= -5:
        label = "略偏空"
        advice = "消息面略負面，進場須謹慎"
    else:
        label = "中性"
        advice = "消息面平淡，依技術面與基本面判斷"

    # 組裝新聞摘要（最多 5 則）
    news_summary = []
    for art in related[:5]:
        sentiment_tag = "正面" if art.sentiment_score > 0 else "負面" if art.sentiment_score < 0 else "中性"
        news_summary.append({
            "title": art.title,
            "source": art.source,
            "published": art.published,
            "link": art.link,
            "sentiment": sentiment_tag,
            "score": round(art.sentiment_score, 1),
        })

    return {
        "score": round(normalized, 1),
        "positive_count": pos_count,
        "negative_count": neg_count,
        "neutral_count": neu_count,
        "total_related": len(related),
        "recent_news": news_summary,
        "sentiment_label": label,
        "advice": advice,
    }


# ── 市場整體情緒 ──

def get_market_sentiment(articles: Optional[List[NewsArticle]] = None) -> dict:
    """
    計算市場整體情緒（不限個股）

    Returns:
        {"score": float, "label": str, "positive_pct": float, "total": int}
    """
    if articles is None:
        articles = fetch_rss_articles()

    if not articles:
        return {"score": 0, "label": "無資料", "positive_pct": 0, "total": 0}

    scores = []
    for art in articles:
        s, _ = analyze_sentiment(art.title, art.description)
        scores.append(s)

    pos = sum(1 for s in scores if s > 0)
    neg = sum(1 for s in scores if s < 0)
    total = len(scores)

    avg = sum(scores) / total
    normalized = max(-100, min(100, avg * 15))

    if normalized >= 15:
        label = "市場氣氛偏多"
    elif normalized <= -15:
        label = "市場氣氛偏空"
    else:
        label = "市場氣氛中性"

    return {
        "score": round(normalized, 1),
        "label": label,
        "positive_pct": round(pos / total * 100, 1) if total else 0,
        "negative_pct": round(neg / total * 100, 1) if total else 0,
        "total": total,
    }


# ── SentimentLayer ──

class SentimentLayer(BaseLayer):
    """消息面情緒分析層"""

    def __init__(self, enabled: bool = True, **kwargs):
        super().__init__("sentiment", enabled)

    def compute_modifier(self, symbol: str, df: pd.DataFrame,
                         sector_id: str = "") -> LayerModifier:
        if not self.enabled:
            return LayerModifier(layer_name=self.name, active=False,
                                 reason="消息面層未啟用")

        # 取得股票名稱（從基本面快取）
        stock_name = ""
        try:
            from .fundamental import fetch_twse_pe_all, _strip_tw
            all_pe = fetch_twse_pe_all()
            code = _strip_tw(symbol)
            info = all_pe.get(code)
            if info:
                stock_name = info.get("name", "")
        except Exception:
            pass

        # 抓取新聞並分析情緒
        articles = fetch_rss_articles()
        if not articles:
            return LayerModifier(
                layer_name=self.name, active=False,
                reason="無法取得新聞資料",
            )

        sentiment = get_stock_sentiment(symbol, stock_name, articles)
        market = get_market_sentiment(articles)

        result = LayerModifier(layer_name=self.name)
        result.details = {
            "stock_sentiment": sentiment,
            "market_sentiment": market,
        }

        score = sentiment["score"]
        total = sentiment["total_related"]

        if total == 0:
            # 無相關新聞 → 用市場整體情緒微調
            m_score = market["score"]
            if m_score >= 20:
                result.buy_multiplier = 1.05
                result.reason = f"無個股新聞｜{market['label']}（{m_score:.0f}分）"
            elif m_score <= -20:
                result.buy_multiplier = 0.95
                result.reason = f"無個股新聞｜{market['label']}（{m_score:.0f}分）"
            else:
                result.reason = f"無個股新聞｜{market['label']}"
            return result

        # 有相關新聞 → 根據情緒分數調整
        if score >= 40:
            result.buy_multiplier = 1.2
            result.buy_offset = 5.0
            result.sell_multiplier = 0.8
            result.reason = f"消息面強烈偏多（{score:.0f}分, {total}則相關新聞）"
        elif score >= 20:
            result.buy_multiplier = 1.1
            result.buy_offset = 3.0
            result.reason = f"消息面偏多（{score:.0f}分, {total}則）"
        elif score >= 5:
            result.buy_multiplier = 1.05
            result.reason = f"消息面略正面（{score:.0f}分, {total}則）"
        elif score <= -40:
            result.buy_multiplier = 0.7
            result.sell_multiplier = 1.2
            result.sell_offset = 5.0
            result.reason = f"消息面強烈偏空（{score:.0f}分, {total}則）—建議觀望"
        elif score <= -20:
            result.buy_multiplier = 0.85
            result.sell_multiplier = 1.1
            result.reason = f"消息面偏空（{score:.0f}分, {total}則）—謹慎操作"
        elif score <= -5:
            result.buy_multiplier = 0.95
            result.reason = f"消息面略負面（{score:.0f}分, {total}則）"
        else:
            result.reason = f"消息面中性（{score:.0f}分, {total}則）"

        return result


# 註冊到 LayerRegistry
LayerRegistry.register("sentiment", SentimentLayer)
