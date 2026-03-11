"""Bollinger Bands (布林帶) 插件"""

import pandas as pd
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('bollinger')
class BollingerIndicator(BaseIndicator):
    """
    Bollinger Bands
    
    買入：價格觸及下軌（超賣），尤其是帶寬收窄後爆發
    賣出：價格觸及上軌（超買）
    """

    def __init__(self, max_score: float = 15.0, params: dict = None):
        default_params = {
            'period': 20,
            'std_dev': 2.0,
        }
        if params:
            default_params.update(params)
        super().__init__(name='Bollinger Bands', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        period = self.params['period']
        std_dev = self.params['std_dev']
        
        # 手寫布林帶公式
        df['bb_middle'] = df['close'].rolling(window=period).mean()
        df['bb_std'] = df['close'].rolling(window=period).std()
        df['bb_upper'] = df['bb_middle'] + (df['bb_std'] * std_dev)
        df['bb_lower'] = df['bb_middle'] - (df['bb_std'] * std_dev)
        
        df['bb_bandwidth'] = (df['bb_upper'] - df['bb_lower']) / df['bb_middle']
        df['bb_percent'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])
        
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        required = ['bb_lower', 'bb_middle', 'bb_upper', 'bb_percent']
        if not all(c in df.columns for c in required):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "布林帶數據不足"
            )

        close = df['close'].iloc[-1]
        bb_lower = df['bb_lower'].iloc[-1]
        bb_upper = df['bb_upper'].iloc[-1]
        bb_middle = df['bb_middle'].iloc[-1]
        bb_pct = df['bb_percent'].iloc[-1]

        if any(pd.isna(v) for v in [close, bb_lower, bb_upper, bb_pct]):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "布林帶數據不足"
            )

        details = {
            'close': close, 'bb_lower': bb_lower, 'bb_upper': bb_upper,
            'bb_middle': bb_middle, 'bb_percent': bb_pct,
        }

        # 價格跌破下軌
        if close <= bb_lower:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, bb_pct, details,
                f"價格跌破布林帶下軌，%B={bb_pct:.2f} → 極度超賣"
            )
        # 價格接近下軌 (bb_percent < 0.1)
        elif bb_pct < 0.1:
            score = self._scale_score(self.max_score * 0.8)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, bb_pct, details,
                f"價格接近布林帶下軌，%B={bb_pct:.2f}"
            )
        # 價格在下軌附近且回升
        elif bb_pct < 0.2:
            score = self._scale_score(self.max_score * 0.5)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, bb_pct, details,
                f"價格在布林帶下部區域，%B={bb_pct:.2f}"
            )
        # 價格突破上軌
        elif close >= bb_upper:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, bb_pct, details,
                f"價格突破布林帶上軌，%B={bb_pct:.2f} → 極度超買"
            )
        # 價格接近上軌
        elif bb_pct > 0.9:
            score = self._scale_score(self.max_score * 0.8)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, bb_pct, details,
                f"價格接近布林帶上軌，%B={bb_pct:.2f}"
            )
        elif bb_pct > 0.8:
            score = self._scale_score(self.max_score * 0.5)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, bb_pct, details,
                f"價格在布林帶上部區域，%B={bb_pct:.2f}"
            )
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, bb_pct, details,
                f"價格在布林帶中部，%B={bb_pct:.2f}"
            )
