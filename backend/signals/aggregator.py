"""
信號聚合引擎

負責：
1. 載入所有指標插件
2. 對 DataFrame 執行所有指標計算
3. 聚合多指標信號為統一的信心評分
"""

import sys
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
import pandas as pd

# 確保 import 路徑正確
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indicators.base import BaseIndicator, IndicatorSignal, SignalType
from indicators.registry import IndicatorRegistry

# 匯入所有指標插件以觸發註冊
from indicators import rsi, macd, bollinger, mfi, ema, volume, adx


@dataclass
class AggregatedSignal:
    """聚合後的綜合信號"""
    timestamp: pd.Timestamp
    symbol: str
    timeframe: str
    
    # 買入信號
    buy_score: float = 0.0          # 買入總分 (0–100)
    buy_signals: List[IndicatorSignal] = field(default_factory=list)
    
    # 賣出信號
    sell_score: float = 0.0         # 賣出總分 (0–100)
    sell_signals: List[IndicatorSignal] = field(default_factory=list)
    
    # 中性信號
    neutral_signals: List[IndicatorSignal] = field(default_factory=list)
    
    # 最終判斷
    direction: str = "NEUTRAL"     # BUY, SELL, NEUTRAL
    confidence: float = 0.0        # 信心度 (0–100)
    signal_level: str = "無信號"    # 極強/強/中等/弱/無
    price: float = 0.0
    
    @property
    def all_signals(self) -> List[IndicatorSignal]:
        return self.buy_signals + self.sell_signals + self.neutral_signals

    def summary(self) -> str:
        """產出信號摘要"""
        emoji = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}.get(self.direction, "⚪")
        lines = [
            f"{emoji} [{self.symbol}] {self.timeframe} | {self.direction} | "
            f"信心度: {self.confidence:.1f}分 ({self.signal_level})",
            f"   價格: ${self.price:,.2f}",
            f"   買入分數: {self.buy_score:.1f} | 賣出分數: {self.sell_score:.1f}",
        ]
        if self.buy_signals:
            lines.append("   📈 買入因素:")
            for s in self.buy_signals:
                lines.append(f"      +{s.score:.1f}分 {s.indicator_name}: {s.reason}")
        if self.sell_signals:
            lines.append("   📉 賣出因素:")
            for s in self.sell_signals:
                lines.append(f"      +{s.score:.1f}分 {s.indicator_name}: {s.reason}")
        return "\n".join(lines)


class SignalAggregator:
    """信號聚合器"""

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        """
        Args:
            weights: 指標權重，e.g. {'rsi': 15, 'macd': 20, ...}
        """
        default_weights = {
            'rsi': 15.0, 'macd': 20.0, 'bollinger': 15.0,
            'mfi': 10.0, 'ema_cross': 15.0, 'volume': 15.0, 'adx': 10.0,
        }
        self.weights = weights or default_weights
        self.indicators: List[BaseIndicator] = IndicatorRegistry.create_all(self.weights)
        
        # 信號等級門檻
        self.thresholds = {
            'extreme_strong': 90.0,
            'strong': 70.0,
            'moderate': 50.0,
            'weak': 30.0,
        }

    def calculate_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """在 DataFrame 上執行所有指標計算"""
        for indicator in self.indicators:
            try:
                df = indicator.calculate(df)
            except Exception as e:
                print(f"  ⚠️ {indicator.name} 計算錯誤: {e}")
        return df

    def generate_signals(self, df: pd.DataFrame, symbol: str = "",
                         timeframe: str = "") -> AggregatedSignal:
        """產出聚合信號"""
        result = AggregatedSignal(
            timestamp=df.index[-1] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now(),
            symbol=symbol,
            timeframe=timeframe,
            price=df['close'].iloc[-1],
        )

        for indicator in self.indicators:
            try:
                signal = indicator.generate_signal(df)
                if signal.signal_type in (SignalType.STRONG_BUY, SignalType.BUY):
                    result.buy_score += signal.score
                    result.buy_signals.append(signal)
                elif signal.signal_type in (SignalType.STRONG_SELL, SignalType.SELL):
                    result.sell_score += signal.score
                    result.sell_signals.append(signal)
                else:
                    result.neutral_signals.append(signal)
            except Exception as e:
                print(f"  ⚠️ {indicator.name} 信號生成錯誤: {e}")

        # 計算最終方向和信心度
        if result.buy_score > result.sell_score:
            result.direction = "BUY"
            result.confidence = result.buy_score
        elif result.sell_score > result.buy_score:
            result.direction = "SELL"
            result.confidence = result.sell_score
        else:
            result.direction = "NEUTRAL"
            result.confidence = 0

        # 設定信號等級
        if result.confidence >= self.thresholds['extreme_strong']:
            result.signal_level = "極強信號"
        elif result.confidence >= self.thresholds['strong']:
            result.signal_level = "強信號"
        elif result.confidence >= self.thresholds['moderate']:
            result.signal_level = "中等信號"
        elif result.confidence >= self.thresholds['weak']:
            result.signal_level = "弱信號"
        else:
            result.signal_level = "無信號"

        return result

    def analyze(self, df: pd.DataFrame, symbol: str = "",
                timeframe: str = "") -> AggregatedSignal:
        """一站式分析：計算指標 + 產出信號"""
        df = self.calculate_all(df)
        return self.generate_signals(df, symbol, timeframe)
