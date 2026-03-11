"""Volume (成交量分析) 插件"""

import pandas as pd
import numpy as np
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('volume')
class VolumeIndicator(BaseIndicator):
    """
    成交量綜合分析
    
    包含：OBV (On-Balance Volume), 成交量均線, 量能爆發偵測
    """

    def __init__(self, max_score: float = 15.0, params: dict = None):
        default_params = {
            'ma_period': 20,
            'spike_multiplier': 2.0,   # 量能爆發倍數
            'obv_ma_period': 20,
        }
        if params:
            default_params.update(params)
        super().__init__(name='Volume', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        ma_period = self.params['ma_period']
        
        # 成交量移動平均
        df['volume_ma'] = df['volume'].rolling(window=ma_period).mean()
        # 成交量比率
        df['volume_ratio'] = df['volume'] / df['volume_ma']
        
        # 手寫 OBV 公式
        diff = df['close'].diff()
        direction = np.sign(diff).fillna(0)
        df['obv'] = (direction * df['volume']).cumsum()
        
        df['obv_ma'] = df['obv'].rolling(window=self.params['obv_ma_period']).mean()
        # 判斷陽線陰線
        df['is_bullish'] = df['close'] > df['open']
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        required = ['volume_ma', 'volume_ratio', 'is_bullish']
        if not all(c in df.columns for c in required):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "成交量數據不足"
            )

        vol = df['volume'].iloc[-1]
        vol_ratio = df['volume_ratio'].iloc[-1]
        is_bullish = df['is_bullish'].iloc[-1]
        spike_mult = self.params['spike_multiplier']

        if pd.isna(vol_ratio):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "成交量數據不足"
            )

        details = {
            'volume': vol,
            'volume_ratio': vol_ratio,
            'is_bullish': bool(is_bullish),
        }

        # OBV 趨勢分析
        obv_bullish = False
        obv_bearish = False
        if 'obv' in df.columns and 'obv_ma' in df.columns:
            obv = df['obv'].iloc[-1]
            obv_ma = df['obv_ma'].iloc[-1]
            if not pd.isna(obv) and not pd.isna(obv_ma):
                obv_bullish = obv > obv_ma
                obv_bearish = obv < obv_ma
                details['obv'] = obv
                details['obv_ma'] = obv_ma

        # 量能爆發 + 陽線 = 強烈買入信號
        if vol_ratio >= spike_mult and is_bullish:
            score = self._scale_score(self.max_score)
            extra = "（OBV 確認多方趨勢）" if obv_bullish else ""
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, vol_ratio, details,
                f"量能爆發 {vol_ratio:.1f}x + 陽線 → 多方主力進場{extra}"
            )
        # 量能爆發 + 陰線 = 強烈賣出信號
        elif vol_ratio >= spike_mult and not is_bullish:
            score = self._scale_score(self.max_score)
            extra = "（OBV 確認空方趨勢）" if obv_bearish else ""
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, vol_ratio, details,
                f"量能爆發 {vol_ratio:.1f}x + 陰線 → 空方主力出貨{extra}"
            )
        # 放量 + 陽線
        elif vol_ratio >= 1.5 and is_bullish:
            score = self._scale_score(self.max_score * 0.6)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, vol_ratio, details,
                f"成交量放大 {vol_ratio:.1f}x + 陽線"
            )
        # 放量 + 陰線
        elif vol_ratio >= 1.5 and not is_bullish:
            score = self._scale_score(self.max_score * 0.6)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, vol_ratio, details,
                f"成交量放大 {vol_ratio:.1f}x + 陰線"
            )
        # OBV 趨勢確認（輕微信號）
        elif obv_bullish:
            score = self._scale_score(self.max_score * 0.25)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, vol_ratio, details,
                f"OBV 處於上升趨勢，量能正常（{vol_ratio:.1f}x）"
            )
        elif obv_bearish:
            score = self._scale_score(self.max_score * 0.25)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, vol_ratio, details,
                f"OBV 處於下降趨勢，量能正常（{vol_ratio:.1f}x）"
            )
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, vol_ratio, details,
                f"成交量正常（{vol_ratio:.1f}x），無明顯信號"
            )
