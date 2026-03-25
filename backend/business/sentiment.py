from enum import Enum
import random
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
        # 歷史/突發事件模板
        self.event_templates = [
            {"text": "💥 地緣政治風險急劇升溫", "impact": "global", "score": -20, "term": EventDuration.SHORT, "type": "空", "tag": "WAR"},
            {"text": "🚀 Elon Musk 重申對狗狗幣的長期支持", "impact": "DOGE/USDT", "score": 25, "term": EventDuration.LONG, "type": "多", "tag": "ELON"},
            {"text": "🏦 聯準會放出貨幣寬鬆（QE）風向球", "impact": "global", "score": 15, "term": EventDuration.LONG, "type": "多", "tag": "FED"},
            {"text": "📉 美國非農數據大幅超出預期，恐延後降息", "impact": "global", "score": -10, "term": EventDuration.SHORT, "type": "空", "tag": "ECON"},
            {"text": "🐳 傳聞某大國正考慮將 BTC 列入儲備資產", "impact": "BTC/USDT", "score": 30, "term": EventDuration.LONG, "type": "多", "tag": "NATION"},
        ]
        
        # 預計發生的重大事件 (倒數計時用)
        # 實際應對接全球財經日曆 API
        self.scheduled_events = [
            {"name": "🇺🇸 FOMC 利率決議", "date": (datetime.now() + timedelta(days=2, hours=5)).isoformat(), "impact": "global", "warning": "利多出盡可能"},
            {"name": "📉 美國 CPI 消費者物價指數公佈", "date": (datetime.now() + timedelta(days=5, hours=3)).isoformat(), "impact": "global", "warning": "波動性預警"},
            {"name": "🪙 比特幣下一輪產量減半 (Halving)", "date": (datetime.now() + timedelta(days=25)).isoformat(), "impact": "BTC/USDT", "warning": "長期看漲趨勢"},
        ]
        
        self.current_event = None

    def get_latest_sentiment(self) -> Optional[Dict]:
        """隨機模擬一個突發事件並進行時效判定"""
        if random.random() < 0.15: # 觸發率
            raw = random.choice(self.event_templates)
            self.current_event = {
                "text": raw["text"],
                "tag": raw["tag"],
                "score": raw["score"],
                "impact": raw["impact"],
                "analysis": f"{raw['term'].value}{raw['type']}", # e.g. "短多", "長空"
                "timestamp": datetime.now().strftime("%H:%M:%S")
            }
        else:
            self.current_event = None
            
        return {
            "current": self.current_event,
            "scheduled": self.scheduled_events
        }

    def check_event_proximity(self, minutes: int = 15) -> Optional[Dict]:
        """檢查是否有重大事件即將在 N 分鐘內發生 (避險觸發器)"""
        now = datetime.now()
        for event in self.scheduled_events:
            event_date = datetime.fromisoformat(event["date"])
            diff = event_date - now
            # 如果事件在未來 0 ~ minutes 分鐘內，或者剛發生過 5 分鐘內
            if timedelta(minutes=-5) <= diff <= timedelta(minutes=minutes):
                return event
        return None

    def apply_sentiment_to_score(self, symbol: str, base_score: float) -> float:
        """根據目前事件計算分數修正"""
        if not self.current_event:
            return base_score
            
        impact = self.current_event["impact"]
        score_mod = self.current_event["score"]
        
        # 如果是全球事件，影響所有幣種
        if impact == "global":
            return base_score + score_mod
        
        # 如果是特定資產，且與當前查詢資產匹配
        if impact == symbol:
            return base_score + score_mod
            
        # 若是其他特定資產，對本資產影響較小 (間接影響)
        return base_score + (score_mod * 0.1)

sentiment_engine = SentimentEngine()
