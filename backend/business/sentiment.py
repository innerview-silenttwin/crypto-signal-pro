from enum import Enum
import random
import urllib.parse
from typing import Dict, List, Optional
from datetime import datetime, timedelta

class EventDuration(Enum):
    SHORT = "短"  # 1-3 天影響
    LONG = "長"   # 數週至數月影響

class SentimentEngine:
    """
    情緒引擎 (Phase 3 強化版)
    支援：短多、短空、長多、長空 判定，以及事件倒數計時與避險模式
    """

    def __init__(self):
        # 歷史/突發事件模板（加入 search_query 供前端產生連結）
        self.event_templates = [
            {"text": "💥 地緣政治風險急劇升溫", "impact": "global", "score": -20, "term": EventDuration.SHORT, "type": "空", "tag": "WAR", "query": "geopolitical risk market impact"},
            {"text": "🚀 Elon Musk 重申對狗狗幣的長期支持", "impact": "DOGE/USDT", "score": 25, "term": EventDuration.LONG, "type": "多", "tag": "ELON", "query": "Elon Musk Dogecoin"},
            {"text": "🏦 聯準會放出貨幣寬鬆（QE）風向球", "impact": "global", "score": 15, "term": EventDuration.LONG, "type": "多", "tag": "FED", "query": "Federal Reserve QE monetary policy"},
            {"text": "📉 美國非農數據大幅超出預期，恐延後降息", "impact": "global", "score": -10, "term": EventDuration.SHORT, "type": "空", "tag": "ECON", "query": "US nonfarm payroll rate cut delay"},
            {"text": "🐳 傳聞某大國正考慮將 BTC 列入儲備資產", "impact": "BTC/USDT", "score": 30, "term": EventDuration.LONG, "type": "多", "tag": "NATION", "query": "Bitcoin national reserve adoption"},
        ]

        # 預計發生的重大事件 (倒數計時用)
        # 實際應對接全球財經日曆 API
        self.scheduled_events = [
            {"name": "🇺🇸 FOMC 利率決議", "date": (datetime.now() + timedelta(days=2, hours=5)).isoformat(), "impact": "global", "warning": "利多出盡可能", "query": "FOMC interest rate decision"},
            {"name": "📉 美國 CPI 消費者物價指數公佈", "date": (datetime.now() + timedelta(days=5, hours=3)).isoformat(), "impact": "global", "warning": "波動性預警", "query": "US CPI consumer price index"},
            {"name": "🪙 比特幣下一輪產量減半 (Halving)", "date": (datetime.now() + timedelta(days=25)).isoformat(), "impact": "BTC/USDT", "warning": "長期看漲趨勢", "query": "Bitcoin halving 2025"},
        ]

        self.current_event = None
        self._last_triggered = None  # 上次觸發時間

    def get_latest_sentiment(self) -> Optional[Dict]:
        """隨機模擬一個突發事件；觸發後持續顯示 2 分鐘"""
        now = datetime.now()

        # 15% 機率觸發新事件
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

        # 若超過 2 分鐘未觸發新事件，才清除
        if self._last_triggered and (now - self._last_triggered).total_seconds() > 120:
            self.current_event = None
            self._last_triggered = None

        # scheduled events 也加上連結
        scheduled_with_links = []
        for evt in self.scheduled_events:
            e = dict(evt)
            e["link"] = "https://www.google.com/search?q=" + urllib.parse.quote(evt["query"] + " latest news")
            scheduled_with_links.append(e)

        return {
            "current": self.current_event,
            "scheduled": scheduled_with_links
        }

    def check_event_proximity(self, minutes: int = 15) -> Optional[Dict]:
        """檢查是否有重大事件即將在 N 分鐘內發生 (避險觸發器)"""
        now = datetime.now()
        for event in self.scheduled_events:
            event_date = datetime.fromisoformat(event["date"])
            diff = event_date - now
            if timedelta(minutes=-5) <= diff <= timedelta(minutes=minutes):
                return event
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
