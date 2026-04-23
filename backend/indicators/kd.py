"""KD 隨機指標 (Stochastic Oscillator) 插件"""

import pandas as pd
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('kd')
class KDIndicator(BaseIndicator):
    """
    KD 隨機指標 (Stochastic Oscillator)

    注意：這是原始的 Stochastic，和 Stochastic RSI 不同。
    RSV = (收盤價 - N日最低) / (N日最高 - N日最低) × 100
    K = RSV 的平滑移動平均
    D = K 的平滑移動平均

    買入：K 線在超賣區（<20）且上穿 D 線（黃金交叉）
    賣出：K 線在超買區（>80）且下穿 D 線（死亡交叉）
    """

    def __init__(self, max_score: float = 12.0, params: dict = None):
        default_params = {
            'period': 9,
            'smooth_k': 3,
            'smooth_d': 3,
            'oversold': 20,
            'overbought': 80,
        }
        if params:
            default_params.update(params)
        super().__init__(name='KD', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        p = self.params['period']
        smooth_k = self.params['smooth_k']
        smooth_d = self.params['smooth_d']

        low_min = df['low'].rolling(window=p, min_periods=p).min()
        high_max = df['high'].rolling(window=p, min_periods=p).max()
        denom = (high_max - low_min).replace(0, float('nan'))
        rsv = (df['close'] - low_min) / denom * 100

        df['kd_k'] = rsv.ewm(span=smooth_k, adjust=False).mean()
        df['kd_d'] = df['kd_k'].ewm(span=smooth_d, adjust=False).mean()
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        if not all(c in df.columns for c in ['kd_k', 'kd_d']):
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "KD 數據不足")
        if len(df) < 2:
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "KD 數據不足")

        k = df['kd_k'].iloc[-1]
        d = df['kd_d'].iloc[-1]
        prev_k = df['kd_k'].iloc[-2]
        prev_d = df['kd_d'].iloc[-2]

        if any(pd.isna(v) for v in [k, d, prev_k, prev_d]):
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "KD 數據不足")

        details = {'kd_k': round(k, 2), 'kd_d': round(d, 2)}
        oversold = self.params['oversold']
        overbought = self.params['overbought']
        golden_cross = prev_k <= prev_d and k > d
        death_cross = prev_k >= prev_d and k < d

        if k < oversold and golden_cross:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, k, details,
                f"KD K={k:.1f} 超賣區黃金交叉（K 上穿 D）"
            )
        elif k < oversold and k > prev_k:
            score = self._scale_score(self.max_score * 0.6)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, k, details,
                f"KD K={k:.1f} 超賣區回升中"
            )
        elif k < oversold:
            score = self._scale_score(self.max_score * 0.35)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, k, details,
                f"KD K={k:.1f} 處於超賣區（<{oversold}）"
            )
        elif k > overbought and death_cross:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, k, details,
                f"KD K={k:.1f} 超買區死亡交叉（K 下穿 D）"
            )
        elif k > overbought and k < prev_k:
            score = self._scale_score(self.max_score * 0.6)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, k, details,
                f"KD K={k:.1f} 超買區回落中"
            )
        elif k > overbought:
            score = self._scale_score(self.max_score * 0.35)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, k, details,
                f"KD K={k:.1f} 處於超買區（>{overbought}）"
            )
        elif golden_cross and k < 50:
            score = self._scale_score(self.max_score * 0.3)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, k, details,
                f"KD K={k:.1f} 低位黃金交叉"
            )
        elif death_cross and k > 50:
            score = self._scale_score(self.max_score * 0.3)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, k, details,
                f"KD K={k:.1f} 高位死亡交叉"
            )
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, k, details,
                f"KD K={k:.1f} D={d:.1f} 中性區間"
            )
