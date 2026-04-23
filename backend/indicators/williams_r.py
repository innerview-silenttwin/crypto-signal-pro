"""威廉指標 (Williams %R) 插件"""

import pandas as pd
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('williams_r')
class WilliamsRIndicator(BaseIndicator):
    """
    Williams %R - 威廉指標

    Williams %R = (最高價 - 收盤價) / (最高價 - 最低價) × (-100)
    範圍：-100 ~ 0

    買入：Williams %R < -80（超賣區）
    賣出：Williams %R > -20（超買區）
    """

    def __init__(self, max_score: float = 10.0, params: dict = None):
        default_params = {
            'period': 14,
            'oversold': -80,
            'overbought': -20,
            'extreme_oversold': -95,
            'extreme_overbought': -5,
        }
        if params:
            default_params.update(params)
        super().__init__(name='Williams %R', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        p = self.params['period']
        hh = df['high'].rolling(window=p, min_periods=p).max()
        ll = df['low'].rolling(window=p, min_periods=p).min()
        denom = (hh - ll).replace(0, float('nan'))
        df['williams_r'] = (hh - df['close']) / denom * -100
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        if 'williams_r' not in df.columns or df['williams_r'].isna().all():
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "Williams %R 數據不足"
            )
        if len(df) < 2:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "Williams %R 數據不足"
            )

        wr = df['williams_r'].iloc[-1]
        prev_wr = df['williams_r'].iloc[-2]

        if pd.isna(wr) or pd.isna(prev_wr):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "Williams %R 數據不足"
            )

        details = {'williams_r': round(wr, 2)}
        rising = wr > prev_wr

        if wr <= self.params['extreme_oversold'] and rising:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, wr, details,
                f"Williams %R={wr:.1f} 極端超賣反彈"
            )
        elif wr <= self.params['oversold'] and rising:
            score = self._scale_score(self.max_score * 0.7)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, wr, details,
                f"Williams %R={wr:.1f} 超賣區回升中"
            )
        elif wr <= self.params['oversold']:
            score = self._scale_score(self.max_score * 0.35)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, wr, details,
                f"Williams %R={wr:.1f} 處於超賣區（<{self.params['oversold']}）"
            )
        elif wr >= self.params['extreme_overbought'] and not rising:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, wr, details,
                f"Williams %R={wr:.1f} 極端超買回落"
            )
        elif wr >= self.params['overbought'] and not rising:
            score = self._scale_score(self.max_score * 0.7)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, wr, details,
                f"Williams %R={wr:.1f} 超買區回落中"
            )
        elif wr >= self.params['overbought']:
            score = self._scale_score(self.max_score * 0.35)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, wr, details,
                f"Williams %R={wr:.1f} 處於超買區（>{self.params['overbought']}）"
            )
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, wr, details,
                f"Williams %R={wr:.1f} 中性區間"
            )
