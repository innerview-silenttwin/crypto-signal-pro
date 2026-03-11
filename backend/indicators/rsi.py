"""RSI (相對強弱指標) 插件"""

import pandas as pd
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('rsi')
class RSIIndicator(BaseIndicator):
    """
    RSI - Relative Strength Index
    
    買入：RSI < 30（超賣區）
    賣出：RSI > 70（超買區）
    極端買入：RSI < 20
    極端賣出：RSI > 80
    """

    def __init__(self, max_score: float = 15.0, params: dict = None):
        default_params = {
            'period': 14,
            'oversold': 30,
            'overbought': 70,
            'extreme_oversold': 20,
            'extreme_overbought': 80,
        }
        if params:
            default_params.update(params)
        super().__init__(name='RSI', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        period = self.params['period']
        
        # 手寫 RSI 公式 (Standard Wilder's Smoothing)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        
        avg_gain = gain.rolling(window=period, min_periods=period).mean()
        avg_loss = loss.rolling(window=period, min_periods=period).mean()
        
        # Wilder's smoothing version
        # for i in range(period, len(df)):
        #     avg_gain.iloc[i] = (avg_gain.iloc[i-1] * (period - 1) + gain.iloc[i]) / period
        #     avg_loss.iloc[i] = (avg_loss.iloc[i-1] * (period - 1) + loss.iloc[i]) / period
        
        rs = avg_gain / avg_loss
        df[f'rsi_{period}'] = 100 - (100 / (1 + rs))
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        period = self.params['period']
        col = f'rsi_{period}'
        
        if col not in df.columns or df[col].isna().all():
            return IndicatorSignal(
                indicator_name=self.name, signal_type=SignalType.NEUTRAL,
                score=0, value=0, details={}, reason="RSI 數據不足"
            )

        rsi = df[col].iloc[-1]
        prev_rsi = df[col].iloc[-2] if len(df) > 1 else rsi

        if pd.isna(rsi):
            return IndicatorSignal(
                indicator_name=self.name, signal_type=SignalType.NEUTRAL,
                score=0, value=0, details={}, reason="RSI 數據不足"
            )

        details = {'rsi': rsi, 'prev_rsi': prev_rsi}

        # 極端超賣
        if rsi < self.params['extreme_oversold']:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, rsi, details,
                f"RSI={rsi:.1f} 極端超賣區（<{self.params['extreme_oversold']}）"
            )
        # 超賣
        elif rsi < self.params['oversold']:
            ratio = (self.params['oversold'] - rsi) / (self.params['oversold'] - self.params['extreme_oversold'])
            score = self._scale_score(self.max_score * (0.5 + 0.5 * ratio))
            return IndicatorSignal(
                self.name, SignalType.BUY, score, rsi, details,
                f"RSI={rsi:.1f} 進入超賣區（<{self.params['oversold']}）"
            )
        # 極端超買
        elif rsi > self.params['extreme_overbought']:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, rsi, details,
                f"RSI={rsi:.1f} 極端超買區（>{self.params['extreme_overbought']}）"
            )
        # 超買
        elif rsi > self.params['overbought']:
            ratio = (rsi - self.params['overbought']) / (self.params['extreme_overbought'] - self.params['overbought'])
            score = self._scale_score(self.max_score * (0.5 + 0.5 * ratio))
            return IndicatorSignal(
                self.name, SignalType.SELL, score, rsi, details,
                f"RSI={rsi:.1f} 進入超買區（>{self.params['overbought']}）"
            )
        # RSI 接近超賣（反轉上升中）
        elif rsi < 40 and prev_rsi < rsi:
            score = self._scale_score(self.max_score * 0.3)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, rsi, details,
                f"RSI={rsi:.1f} 從低位回升中"
            )
        # RSI 接近超買（開始下降）
        elif rsi > 60 and prev_rsi > rsi:
            score = self._scale_score(self.max_score * 0.3)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, rsi, details,
                f"RSI={rsi:.1f} 從高位開始回落"
            )
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, rsi, details,
                f"RSI={rsi:.1f} 處於中性區間"
            )
