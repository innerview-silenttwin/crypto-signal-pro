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
from enum import Enum

class MarketType(Enum):
    CRYPTO = "crypto"
    STOCK = "stock"
    FUTURES = "futures"
    US_STOCK = "us_stock"

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
    change_24h: float = 0.0       # 今日漲跌幅 (%)

    # 分析層修正
    layer_modifiers: List = field(default_factory=list)  # LayerModifier list
    raw_buy_score: float = 0.0     # 修正前買入分
    raw_sell_score: float = 0.0    # 修正前賣出分
    regime: str = ""               # 當前盤勢
    
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

    def __init__(self, market_type: MarketType = MarketType.CRYPTO, weights: Optional[Dict[str, float]] = None):
        """
        Args:
            market_type: 市場類型，影響權重配置
            weights: 指標權重，e.g. {'rsi': 15, 'macd': 20, ...}
        """
        self.market_type = market_type
        
        # 不同市場的預設加權策略
        if market_type == MarketType.STOCK:
            default_weights = {
                'rsi': 10.0, 'macd': 15.0, 'bollinger': 10.0,
                'mfi': 15.0, 'ema_cross': 15.0, 'volume': 25.0, 'adx': 10.0, # 台股更重成交量
            }
        elif market_type == MarketType.FUTURES:
            default_weights = {
                'rsi': 15.0, 'macd': 15.0, 'bollinger': 15.0,
                'mfi': 10.0, 'ema_cross': 20.0, 'volume': 15.0, 'adx': 10.0, # 期貨偏向趨勢追蹤
            }
        else: # Crypto (預設)
            default_weights = {
                'rsi': 20.0, 'macd': 20.0, 'bollinger': 10.0,
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
                timeframe: str = "", layers=None,
                sector_id: str = "") -> AggregatedSignal:
        """一站式分析：計算指標 + 產出信號 + 套用分析層修正"""
        df = self.calculate_all(df)
        signal = self.generate_signals(df, symbol, timeframe)

        # 套用分析層修正
        if layers:
            signal.raw_buy_score = signal.buy_score
            signal.raw_sell_score = signal.sell_score

            for layer in layers:
                if not layer.enabled:
                    continue
                try:
                    modifier = layer.compute_modifier(symbol, df, sector_id)
                    if not modifier.active:
                        continue

                    signal.layer_modifiers.append(modifier)
                    if modifier.regime:
                        signal.regime = modifier.regime

                    # 套用乘數和偏移
                    signal.buy_score = (
                        signal.buy_score * modifier.buy_multiplier
                        + modifier.buy_offset
                    )
                    signal.sell_score = (
                        signal.sell_score * modifier.sell_multiplier
                        + modifier.sell_offset
                    )

                    # 否決控制
                    if modifier.veto_buy:
                        signal.buy_score = min(signal.buy_score, 10)
                    if modifier.veto_sell:
                        signal.sell_score = min(signal.sell_score, 10)

                except Exception as e:
                    print(f"  ⚠️ Layer {layer.name} 錯誤: {e}")

            # 限制在 0-100 範圍
            signal.buy_score = max(0, min(100, signal.buy_score))
            signal.sell_score = max(0, min(100, signal.sell_score))

            # 重新計算方向和信心度
            if signal.buy_score > signal.sell_score:
                signal.direction = "BUY"
                signal.confidence = signal.buy_score
            elif signal.sell_score > signal.buy_score:
                signal.direction = "SELL"
                signal.confidence = signal.sell_score
            else:
                signal.direction = "NEUTRAL"
                signal.confidence = 0

            # 重新設定信號等級
            if signal.confidence >= self.thresholds['extreme_strong']:
                signal.signal_level = "極強信號"
            elif signal.confidence >= self.thresholds['strong']:
                signal.signal_level = "強信號"
            elif signal.confidence >= self.thresholds['moderate']:
                signal.signal_level = "中等信號"
            elif signal.confidence >= self.thresholds['weak']:
                signal.signal_level = "弱信號"
            else:
                signal.signal_level = "無信號"

        return signal
