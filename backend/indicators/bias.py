"""乖離率 (BIAS) 指標插件"""

import pandas as pd
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('bias')
class BiasIndicator(BaseIndicator):
    """
    乖離率 (BIAS) - 價格與移動平均線的偏離程度

    BIAS = (收盤價 - MA_N) / MA_N × 100

    買入：負乖離過大（超賣，均值回歸機會）
    賣出：正乖離過大（超買，回落風險）
    """

    def __init__(self, max_score: float = 12.0, params: dict = None):
        default_params = {
            'periods': [5, 10, 20, 60],
            'primary_period': 20,
            'overbought': 8.0,
            'oversold': -8.0,
            'extreme_overbought': 12.0,
            'extreme_oversold': -12.0,
        }
        if params:
            default_params.update(params)
        super().__init__(name='BIAS', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for p in self.params['periods']:
            ma = df['close'].rolling(window=p, min_periods=p).mean()
            df[f'bias_{p}'] = (df['close'] - ma) / ma * 100
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        col = f'bias_{self.params["primary_period"]}'

        if col not in df.columns or df[col].isna().all():
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "BIAS 數據不足"
            )

        bias = df[col].iloc[-1]
        if pd.isna(bias):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "BIAS 數據不足"
            )

        details = {'bias': round(bias, 2), 'period': self.params['primary_period']}

        if bias <= self.params['extreme_oversold']:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, bias, details,
                f"乖離率 {bias:.1f}% 極端超賣（<{self.params['extreme_oversold']}%）"
            )
        elif bias <= self.params['oversold']:
            ratio = (self.params['oversold'] - bias) / (self.params['oversold'] - self.params['extreme_oversold'])
            score = self._scale_score(self.max_score * (0.5 + 0.5 * ratio))
            return IndicatorSignal(
                self.name, SignalType.BUY, score, bias, details,
                f"乖離率 {bias:.1f}% 超賣區（<{self.params['oversold']}%）"
            )
        elif bias >= self.params['extreme_overbought']:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, bias, details,
                f"乖離率 {bias:.1f}% 極端超買（>{self.params['extreme_overbought']}%）"
            )
        elif bias >= self.params['overbought']:
            ratio = (bias - self.params['overbought']) / (self.params['extreme_overbought'] - self.params['overbought'])
            score = self._scale_score(self.max_score * (0.5 + 0.5 * ratio))
            return IndicatorSignal(
                self.name, SignalType.SELL, score, bias, details,
                f"乖離率 {bias:.1f}% 超買區（>{self.params['overbought']}%）"
            )
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, bias, details,
                f"乖離率 {bias:.1f}% 中性區間"
            )
