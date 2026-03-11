"""ADX (平均趨向指標) 插件"""

import pandas as pd
import numpy as np
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('adx')
class ADXIndicator(BaseIndicator):
    """
    ADX - Average Directional Index
    
    用於判斷趨勢強度：
    - ADX > 25: 趨勢明確
    - ADX < 20: 無趨勢（震盪）
    - +DI > -DI: 多方主導
    - -DI > +DI: 空方主導
    """

    def __init__(self, max_score: float = 10.0, params: dict = None):
        default_params = {
            'period': 14,
            'trend_threshold': 25,
            'strong_trend': 40,
        }
        if params:
            default_params.update(params)
        super().__init__(name='ADX', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        period = self.params['period']
        
        # 手寫 ADX 公式
        th = df['high']
        tl = df['low']
        tc = df['close']
        
        tr = pd.concat([
            th - tl,
            (th - tc.shift(1)).abs(),
            (tl - tc.shift(1)).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean() # Simplified smoothing
        
        up_move = th - th.shift(1)
        down_move = tl.shift(1) - tl
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
        
        plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(window=period).mean() / atr)
        minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(window=period).mean() / atr)
        
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        df['adx'] = dx.rolling(window=period).mean()
        df['plus_di'] = plus_di
        df['minus_di'] = minus_di
        
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        required = ['adx', 'plus_di', 'minus_di']
        if not all(c in df.columns for c in required):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "ADX 數據不足"
            )

        adx = df['adx'].iloc[-1]
        plus_di = df['plus_di'].iloc[-1]
        minus_di = df['minus_di'].iloc[-1]

        if any(pd.isna(v) for v in [adx, plus_di, minus_di]):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "ADX 數據不足"
            )

        details = {'adx': adx, 'plus_di': plus_di, 'minus_di': minus_di}
        trend_th = self.params['trend_threshold']
        strong_th = self.params['strong_trend']

        # 強趨勢 + 多方主導
        if adx >= strong_th and plus_di > minus_di:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, adx, details,
                f"ADX={adx:.1f} 強烈趨勢 + 多方主導（+DI={plus_di:.1f} > -DI={minus_di:.1f}）"
            )
        # 強趨勢 + 空方主導
        elif adx >= strong_th and minus_di > plus_di:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, adx, details,
                f"ADX={adx:.1f} 強烈趨勢 + 空方主導（-DI={minus_di:.1f} > +DI={plus_di:.1f}）"
            )
        # 趨勢確立 + 多方
        elif adx >= trend_th and plus_di > minus_di:
            score = self._scale_score(self.max_score * 0.7)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, adx, details,
                f"ADX={adx:.1f} 趨勢確立 + 多方主導"
            )
        # 趨勢確立 + 空方
        elif adx >= trend_th and minus_di > plus_di:
            score = self._scale_score(self.max_score * 0.7)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, adx, details,
                f"ADX={adx:.1f} 趨勢確立 + 空方主導"
            )
        # 無趨勢（震盪市場）
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, adx, details,
                f"ADX={adx:.1f} 趨勢不明確（<{trend_th}），市場震盪中"
            )
