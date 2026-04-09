"""Volume Reversal - 底部/高檔爆量反轉偵測，含破底警示"""

import pandas as pd
import numpy as np
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('volume_reversal')
class VolumeReversalIndicator(BaseIndicator):
    """
    底部/高檔爆量反轉偵測

    核心邏輯：
    - 底部爆大量反轉（買入）：收盤接近 N 日低點 + 量能 ≥ 2.5x 均量 + 陽線
    - 高檔爆大量反轉（賣出）：收盤接近 N 日高點 + 量能 ≥ 2.5x 均量 + 陰線
    - 破底警示（賣出）：今日最低跌破 N 日低點 + 量能 ≥ 1.5x → 禁止進場信號

    破底判斷優先於反轉判斷，保護做多安全。
    """

    def __init__(self, max_score: float = 18.0, params: dict = None):
        default_params = {
            'lookback': 20,              # 滾動高低點天數
            'near_pct': 0.04,            # 距高低點幾 % 內算「接近」
            'reversal_vol_mult': 2.5,    # 爆量反轉倍數（相對 vol_ma）
            'confirm_vol_mult': 1.5,     # 放量確認倍數（稍弱信號）
            'breakdown_vol_mult': 1.5,   # 破底確認量能倍數
            'vol_ma_period': 20,         # 量能均線週期
        }
        if params:
            default_params.update(params)
        super().__init__(name='Volume Reversal', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        vma = self.params['vol_ma_period']
        lb = self.params['lookback']

        df['vr_vol_ma'] = df['volume'].rolling(window=vma).mean()
        df['vr_vol_ratio'] = df['volume'] / df['vr_vol_ma']

        # 滾動高低點：shift(1) 排除當根，避免前視偏差
        df['vr_rolling_low'] = df['low'].shift(1).rolling(window=lb).min()
        df['vr_rolling_high'] = df['high'].shift(1).rolling(window=lb).max()

        df['vr_is_bullish'] = df['close'] > df['open']
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        required = ['vr_vol_ratio', 'vr_rolling_low', 'vr_rolling_high', 'vr_is_bullish']
        if not all(c in df.columns for c in required):
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "Volume Reversal 數據不足")

        close = df['close'].iloc[-1]
        low_today = df['low'].iloc[-1]
        vol_ratio = df['vr_vol_ratio'].iloc[-1]
        rolling_low = df['vr_rolling_low'].iloc[-1]
        rolling_high = df['vr_rolling_high'].iloc[-1]
        is_bullish = df['vr_is_bullish'].iloc[-1]

        if any(pd.isna(v) for v in [vol_ratio, rolling_low, rolling_high]):
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "Volume Reversal 數據不足")

        near_pct = self.params['near_pct']
        rev_mult = self.params['reversal_vol_mult']
        confirm_mult = self.params['confirm_vol_mult']
        breakdown_mult = self.params['breakdown_vol_mult']

        details = {
            'close': round(close, 2),
            'vol_ratio': round(vol_ratio, 2),
            'rolling_low': round(rolling_low, 2),
            'rolling_high': round(rolling_high, 2),
        }

        # ── 優先判斷：破底警示 ──────────────────────────────────────
        # 今日最低跌破 N 日低點 + 放量確認 → 空方突破，不進場
        if low_today < rolling_low and vol_ratio >= breakdown_mult:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, vol_ratio, details,
                f"破底警示：今日低({low_today:.1f}) < {self.params['lookback']}日低({rolling_low:.1f})"
                f" + 量能{vol_ratio:.1f}x → 禁止做多進場"
            )

        low_zone = rolling_low * (1 + near_pct)
        high_zone = rolling_high * (1 - near_pct)

        # ── 底部爆量反轉（買入）──────────────────────────────────────
        if close <= low_zone and vol_ratio >= rev_mult and is_bullish:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, vol_ratio, details,
                f"底部爆量反轉：接近{self.params['lookback']}日低 + 量能{vol_ratio:.1f}x + 陽線 → 強力買入"
            )
        if close <= low_zone and vol_ratio >= confirm_mult and is_bullish:
            score = self._scale_score(self.max_score * 0.6)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, vol_ratio, details,
                f"低點附近放量陽線（量能{vol_ratio:.1f}x），留意反轉"
            )

        # ── 高檔爆量反轉（賣出）──────────────────────────────────────
        if close >= high_zone and vol_ratio >= rev_mult and not is_bullish:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, vol_ratio, details,
                f"高檔爆量反轉：接近{self.params['lookback']}日高 + 量能{vol_ratio:.1f}x + 陰線 → 出貨警示"
            )
        if close >= high_zone and vol_ratio >= confirm_mult and not is_bullish:
            score = self._scale_score(self.max_score * 0.6)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, vol_ratio, details,
                f"高點附近放量陰線（量能{vol_ratio:.1f}x），留意出貨"
            )

        return IndicatorSignal(
            self.name, SignalType.NEUTRAL, 0, vol_ratio, details,
            f"無爆量反轉訊號（量能{vol_ratio:.1f}x）"
        )
