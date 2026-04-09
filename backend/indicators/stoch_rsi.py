"""Stochastic RSI - 隨機RSI，拉回超賣快速偵測"""

import pandas as pd
import numpy as np
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('stoch_rsi')
class StochRSIIndicator(BaseIndicator):
    """
    Stochastic RSI

    比 RSI 更靈敏的超買超賣振盪指標，適合偵測拉回後的進場時機。
    StochRSI_K = SMA( (RSI - RSI_min) / (RSI_max - RSI_min), smooth_k )

    買入：K 線在超賣區（< 0.20）且上穿 D 線 → 拉回反彈訊號
    賣出：K 線在超買區（> 0.80）且下穿 D 線 → 高位回落訊號
    """

    def __init__(self, max_score: float = 12.0, params: dict = None):
        default_params = {
            'rsi_period': 14,
            'stoch_period': 14,
            'smooth_k': 3,
            'smooth_d': 3,
            'oversold': 0.20,
            'overbought': 0.80,
        }
        if params:
            default_params.update(params)
        super().__init__(name='Stoch RSI', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        rsi_period = self.params['rsi_period']
        stoch_period = self.params['stoch_period']
        smooth_k = self.params['smooth_k']
        smooth_d = self.params['smooth_d']

        # 計算 RSI
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        avg_gain = gain.rolling(window=rsi_period, min_periods=rsi_period).mean()
        avg_loss = loss.rolling(window=rsi_period, min_periods=rsi_period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))

        # 計算 Stochastic of RSI
        rsi_min = rsi.rolling(window=stoch_period).min()
        rsi_max = rsi.rolling(window=stoch_period).max()
        rsi_range = (rsi_max - rsi_min).replace(0, np.nan)
        raw_k = (rsi - rsi_min) / rsi_range

        df['stoch_rsi_k'] = raw_k.rolling(window=smooth_k).mean()
        df['stoch_rsi_d'] = df['stoch_rsi_k'].rolling(window=smooth_d).mean()
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        if not all(c in df.columns for c in ['stoch_rsi_k', 'stoch_rsi_d']):
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "StochRSI 數據不足")
        if len(df) < 2:
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "StochRSI 數據不足")

        k = df['stoch_rsi_k'].iloc[-1]
        d = df['stoch_rsi_d'].iloc[-1]
        prev_k = df['stoch_rsi_k'].iloc[-2]
        prev_d = df['stoch_rsi_d'].iloc[-2]

        if any(pd.isna(v) for v in [k, d, prev_k, prev_d]):
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "StochRSI 數據不足")

        details = {'stoch_rsi_k': round(k, 3), 'stoch_rsi_d': round(d, 3)}
        oversold = self.params['oversold']
        overbought = self.params['overbought']
        rising = k > prev_k

        # K 線在超賣區且上穿 D 線（黃金交叉）
        if k < oversold and prev_k <= prev_d and k > d:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, k, details,
                f"StochRSI K={k:.2f} 超賣區上穿 D 線 → 拉回反彈買點"
            )
        # 超賣區且上升中
        if k < oversold and rising:
            score = self._scale_score(self.max_score * 0.65)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, k, details,
                f"StochRSI K={k:.2f} 超賣區回升中"
            )
        # 超賣區（靜止）
        if k < oversold:
            score = self._scale_score(self.max_score * 0.35)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, k, details,
                f"StochRSI K={k:.2f} 處於超賣區"
            )
        # 超買區且下穿 D 線（死亡交叉）
        if k > overbought and prev_k >= prev_d and k < d:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, k, details,
                f"StochRSI K={k:.2f} 超買區下穿 D 線 → 高位回落賣點"
            )
        # 超買區且下降中
        if k > overbought and not rising:
            score = self._scale_score(self.max_score * 0.65)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, k, details,
                f"StochRSI K={k:.2f} 超買區回落中"
            )

        return IndicatorSignal(
            self.name, SignalType.NEUTRAL, 0, k, details,
            f"StochRSI K={k:.2f} 中性區間"
        )
