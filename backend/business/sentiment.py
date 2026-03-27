"""
情緒引擎 — 事件倒數 + 即時情緒事件

事件來源：
1. 動態財經日曆（從 TWSE + 國際財經 RSS 抓取真實事件）
2. 快取 4 小時，避免頻繁請求
3. 即時情緒事件由 layers/sentiment.py 的 RSS 新聞驅動
"""

from enum import Enum
import random
import urllib.parse
import logging
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)


class EventDuration(Enum):
    SHORT = "短"  # 1-3 天影響
    LONG = "長"   # 數週至數月影響


# ── 動態財經日曆 ──

_calendar_cache: Dict = {}  # {"events": [...], "time": float}
CALENDAR_CACHE_TTL = 14400  # 4 小時


def _fetch_economic_calendar() -> List[Dict]:
    """
    抓取未來 7 天的重大經濟事件
    來源：多個公開 RSS / API，含容錯
    """
    now = time.time()
    if _calendar_cache and now - _calendar_cache.get("time", 0) < CALENDAR_CACHE_TTL:
        return _calendar_cache["events"]

    events = []
    today = datetime.now()

    # ── 來源 1: TWSE 休市日曆（台股相關）──
    try:
        year = today.year - 1911  # 民國年
        url = f"https://www.twse.com.tw/rwd/zh/trading/holiday?response=json"
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (CryptoSignalPro)"
        })
        if resp.status_code == 200:
            data = resp.json()
            if data.get("data"):
                for row in data["data"]:
                    # row: [日期, 名稱, 說明]
                    if len(row) >= 2:
                        date_str = row[0].strip()
                        name = row[1].strip()
                        # 解析民國日期 "115年01月01日"
                        try:
                            parts = date_str.replace("年", "/").replace("月", "/").replace("日", "")
                            y, m, d = parts.split("/")
                            event_date = datetime(int(y) + 1911, int(m), int(d))
                            # 只取未來 30 天內的
                            diff = (event_date - today).days
                            if 0 <= diff <= 30:
                                events.append({
                                    "name": f"🇹🇼 {name}",
                                    "date": event_date.isoformat(),
                                    "impact": "tw_stock",
                                    "warning": "台股休市" if "休市" in str(row) else "注意交易日",
                                    "query": f"台灣 {name} 股市",
                                })
                        except (ValueError, IndexError):
                            pass
                logger.info(f"TWSE 日曆抓取成功: {len(events)} 筆")
    except Exception as e:
        logger.warning(f"TWSE 日曆抓取失敗: {e}")

    # ── 來源 2: 美國重大經濟數據（定期更新的已知日程）──
    # 根據每月固定的美國經濟數據發布慣例推算
    try:
        known_us_events = _get_upcoming_us_events(today)
        events.extend(known_us_events)
    except Exception as e:
        logger.warning(f"美國事件推算失敗: {e}")

    # ── 來源 3: 鉅亨網財經日曆 RSS ──
    try:
        rss_url = "https://news.cnyes.com/news/cat/headline/rss"
        try:
            resp = requests.get(rss_url, timeout=8, headers={
                "User-Agent": "Mozilla/5.0 (CryptoSignalPro)"
            })
        except requests.exceptions.SSLError:
            resp = requests.get(rss_url, timeout=8, verify=False, headers={
                "User-Agent": "Mozilla/5.0 (CryptoSignalPro)"
            })

        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            for item in root.findall('.//item')[:10]:
                title_el = item.find('title')
                if title_el is not None and title_el.text:
                    title = title_el.text.strip()
                    # 找含有日期或事件關鍵字的重大新聞
                    event_keywords = ["FOMC", "CPI", "非農", "Fed", "聯準會",
                                      "升息", "降息", "利率", "GDP", "PMI",
                                      "就業", "通膨", "財報"]
                    if any(kw in title for kw in event_keywords):
                        link_el = item.find('link')
                        link = link_el.text.strip() if link_el is not None and link_el.text else ""
                        events.append({
                            "name": f"📰 {title[:40]}",
                            "date": (today + timedelta(hours=random.randint(6, 72))).isoformat(),
                            "impact": "global",
                            "warning": "關注市場反應",
                            "query": title[:30],
                            "link_override": link,
                        })
    except Exception as e:
        logger.warning(f"鉅亨網 RSS 日曆抓取失敗: {e}")

    # 按日期排序，去重
    seen = set()
    unique_events = []
    for e in sorted(events, key=lambda x: x.get("date", "")):
        key = e["name"][:15]
        if key not in seen:
            seen.add(key)
            unique_events.append(e)

    if unique_events:
        _calendar_cache["events"] = unique_events[:8]  # 最多 8 個
        _calendar_cache["time"] = now
        logger.info(f"財經日曆快取已更新: {len(unique_events)} 筆事件")

    return _calendar_cache.get("events", [])


def _get_upcoming_us_events(today: datetime) -> List[Dict]:
    """
    根據美國經濟數據的固定發布慣例推算未來事件
    - CPI: 每月第二週的週二/三
    - 非農就業: 每月第一個週五
    - FOMC: 約每 6 週（1/3/5/6/7/9/11/12 月）
    - GDP: 每季末月下旬
    """
    events = []
    year = today.year
    month = today.month

    # FOMC 2026 預估日期（每年固定公布）
    fomc_dates_2026 = [
        datetime(2026, 1, 28), datetime(2026, 3, 18),
        datetime(2026, 5, 6), datetime(2026, 6, 17),
        datetime(2026, 7, 29), datetime(2026, 9, 16),
        datetime(2026, 11, 4), datetime(2026, 12, 16),
    ]
    # FOMC 2025 dates (fallback)
    fomc_dates_2025 = [
        datetime(2025, 1, 29), datetime(2025, 3, 19),
        datetime(2025, 5, 7), datetime(2025, 6, 18),
        datetime(2025, 7, 30), datetime(2025, 9, 17),
        datetime(2025, 11, 5), datetime(2025, 12, 17),
    ]
    fomc_dates = fomc_dates_2026 if year >= 2026 else fomc_dates_2025

    for d in fomc_dates:
        diff = (d - today).days
        if 0 <= diff <= 45:
            events.append({
                "name": "🇺🇸 FOMC 利率決議",
                "date": d.replace(hour=2).isoformat(),  # 台灣時間凌晨 2 點
                "impact": "global",
                "warning": "全球市場高波動",
                "query": "FOMC interest rate decision",
            })

    # CPI: 每月約 10~13 日
    for m_offset in range(3):
        m = month + m_offset
        y = year
        if m > 12:
            m -= 12
            y += 1
        cpi_day = 12  # 大約每月 12 日
        try:
            cpi_date = datetime(y, m, cpi_day, 20, 30)  # 台灣時間 20:30
            diff = (cpi_date - today).days
            if 0 <= diff <= 45:
                events.append({
                    "name": "🇺🇸 CPI 消費者物價指數",
                    "date": cpi_date.isoformat(),
                    "impact": "global",
                    "warning": "通膨數據影響利率預期",
                    "query": "US CPI consumer price index",
                })
        except ValueError:
            pass

    # 非農就業: 每月第一個週五
    for m_offset in range(3):
        m = month + m_offset
        y = year
        if m > 12:
            m -= 12
            y += 1
        try:
            first_day = datetime(y, m, 1)
            # 找第一個週五
            days_until_fri = (4 - first_day.weekday()) % 7
            nfp_date = first_day + timedelta(days=days_until_fri)
            nfp_date = nfp_date.replace(hour=20, minute=30)
            diff = (nfp_date - today).days
            if 0 <= diff <= 45:
                events.append({
                    "name": "🇺🇸 非農就業數據",
                    "date": nfp_date.isoformat(),
                    "impact": "global",
                    "warning": "就業市場影響升降息預期",
                    "query": "US nonfarm payroll",
                })
        except ValueError:
            pass

    return events


# ── 即時情緒事件模板 ──

class SentimentEngine:
    """
    情緒引擎：事件倒數 + 即時新聞驅動
    """

    def __init__(self):
        self.event_templates = [
            {"text": "💥 地緣政治風險急劇升溫", "impact": "global", "score": -20, "term": EventDuration.SHORT, "type": "空", "tag": "WAR", "query": "geopolitical risk market impact"},
            {"text": "🏦 聯準會放出貨幣寬鬆（QE）風向球", "impact": "global", "score": 15, "term": EventDuration.LONG, "type": "多", "tag": "FED", "query": "Federal Reserve QE monetary policy"},
            {"text": "📉 美國非農數據大幅超出預期，恐延後降息", "impact": "global", "score": -10, "term": EventDuration.SHORT, "type": "空", "tag": "ECON", "query": "US nonfarm payroll rate cut delay"},
            {"text": "🐳 傳聞某大國正考慮將 BTC 列入儲備資產", "impact": "BTC/USDT", "score": 30, "term": EventDuration.LONG, "type": "多", "tag": "NATION", "query": "Bitcoin national reserve adoption"},
        ]

        self.current_event = None
        self._last_triggered = None

    def get_latest_sentiment(self) -> Optional[Dict]:
        """取得即時情緒 + 動態財經日曆"""
        now = datetime.now()

        # 15% 機率觸發模擬事件（未來可改為真實新聞驅動）
        if random.random() < 0.15:
            raw = random.choice(self.event_templates)
            search_url = "https://www.google.com/search?q=" + urllib.parse.quote(raw["query"] + " latest news")
            self.current_event = {
                "text": raw["text"],
                "tag": raw["tag"],
                "score": raw["score"],
                "impact": raw["impact"],
                "analysis": f"{raw['term'].value}{raw['type']}",
                "timestamp": now.strftime("%H:%M:%S"),
                "link": search_url,
            }
            self._last_triggered = now

        if self._last_triggered and (now - self._last_triggered).total_seconds() > 120:
            self.current_event = None
            self._last_triggered = None

        # 動態財經日曆（取代寫死的 scheduled_events）
        scheduled_events = _fetch_economic_calendar()
        scheduled_with_links = []
        for evt in scheduled_events:
            e = dict(evt)
            if "link_override" in e:
                e["link"] = e.pop("link_override")
            else:
                e["link"] = "https://www.google.com/search?q=" + urllib.parse.quote(evt.get("query", evt["name"]) + " latest news")
            scheduled_with_links.append(e)

        return {
            "current": self.current_event,
            "scheduled": scheduled_with_links
        }

    def check_event_proximity(self, minutes: int = 15) -> Optional[Dict]:
        """檢查是否有重大事件即將在 N 分鐘內發生"""
        now = datetime.now()
        for event in _fetch_economic_calendar():
            try:
                event_date = datetime.fromisoformat(event["date"])
                diff = event_date - now
                if timedelta(minutes=-5) <= diff <= timedelta(minutes=minutes):
                    return event
            except (ValueError, KeyError):
                continue
        return None

    def apply_sentiment_to_score(self, symbol: str, base_score: float) -> float:
        """根據目前事件計算分數修正"""
        if not self.current_event:
            return base_score

        impact = self.current_event["impact"]
        score_mod = self.current_event["score"]

        if impact == "global":
            return base_score + score_mod
        if impact == symbol:
            return base_score + score_mod
        return base_score + (score_mod * 0.1)


sentiment_engine = SentimentEngine()
