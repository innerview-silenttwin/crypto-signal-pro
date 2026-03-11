"""MACD (移動平均收斂發散) 插件"""

import pandas as pd
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('macd')
class MACDIndicator(BaseIndicator):
    """
    MACD - Moving Average Convergence Divergence
    
    買入：MACD線上穿信號線（黃金交叉）+ 柱狀圖翻正
    賣出：MACD線下穿信號線（死亡交叉）+ 柱狀圖翻負
    """

    def __init__(self, max_score: float = 20.0, params: dict = None):
        default_params = {
            'fast': 12,
            'slow': 26,
            'signal': 9,
        }
        if params:
            default_params.update(params)
        super().__init__(name='MACD', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        fast = self.params['fast']
        slow = self.params['slow']
        signal = self.params['signal']
        
        # 手寫 MACD 公式
        ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
        ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
        
        df['macd'] = ema_fast - ema_slow
        df['macd_signal'] = df['macd'].ewm(span=signal, adjust=False).mean()
        df['macd_histogram'] = df['macd'] - df['macd_signal']
        
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        required = ['macd', 'macd_signal', 'macd_histogram']
        if not all(c in df.columns for c in required):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "MACD 數據不足"
            )

        if len(df) < 2:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "MACD 數據不足"
            )

        macd = df['macd'].iloc[-1]
        signal = df['macd_signal'].iloc[-1]
        hist = df['macd_histogram'].iloc[-1]
        prev_macd = df['macd'].iloc[-2]
        prev_signal = df['macd_signal'].iloc[-2]
        prev_hist = df['macd_histogram'].iloc[-2]

        if any(pd.isna(v) for v in [macd, signal, hist, prev_macd, prev_signal, prev_hist]):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "MACD 數據不足"
            )

        details = {
            'macd': macd, 'signal': signal, 'histogram': hist,
            'prev_histogram': prev_hist,
        }

        # 黃金交叉：MACD 由下往上穿越信號線
        golden_cross = prev_macd <= prev_signal and macd > signal
        # 死亡交叉：MACD 由上往下穿越信號線
        death_cross = prev_macd >= prev_signal and macd < signal
        # 柱狀圖翻正
        hist_turn_positive = prev_hist <= 0 and hist > 0
        # 柱狀圖翻負
        hist_turn_negative = prev_hist >= 0 and hist < 0

        # 強烈買入：黃金交叉 + 柱狀圖翻正
        if golden_cross and hist > 0:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, macd, details,
                "MACD 黃金交叉 + 柱狀圖翻正 → 強烈買入"
            )
        # 買入：柱狀圖翻正（非交叉但動能反轉）
        elif hist_turn_positive:
            score = self._scale_score(self.max_score * 0.7)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, macd, details,
                "MACD 柱狀圖翻正，動能反轉向上"
            )
        # 柱狀圖遞增（趨勢增強）
        elif hist > 0 and hist > prev_hist:
            score = self._scale_score(self.max_score * 0.4)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, macd, details,
                "MACD 柱狀圖持續增長，多方動能增強"
            )
        # 強烈賣出：死亡交叉 + 柱狀圖翻負
        elif death_cross and hist < 0:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, macd, details,
                "MACD 死亡交叉 + 柱狀圖翻負 → 強烈賣出"
            )
        # 賣出：柱狀圖翻負
        elif hist_turn_negative:
            score = self._scale_score(self.max_score * 0.7)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, macd, details,
                "MACD 柱狀圖翻負，動能反轉向下"
            )
        # 柱狀圖遞減（空方增強）
        elif hist < 0 and hist < prev_hist:
            score = self._scale_score(self.max_score * 0.4)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, macd, details,
                "MACD 柱狀圖持續下降，空方動能增強"
            )
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, macd, details,
                "MACD 無明顯信號"
            )
