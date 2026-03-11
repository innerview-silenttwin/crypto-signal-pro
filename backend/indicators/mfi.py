"""MFI (資金流量指標) 插件"""

import pandas as pd
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('mfi')
class MFIIndicator(BaseIndicator):
    """
    MFI - Money Flow Index（量價合一的 RSI）
    
    買入：MFI < 20（資金超賣）
    賣出：MFI > 80（資金超買）
    """

    def __init__(self, max_score: float = 10.0, params: dict = None):
        default_params = {
            'period': 14,
            'oversold': 20,
            'overbought': 80,
        }
        if params:
            default_params.update(params)
        super().__init__(name='MFI', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        period = self.params['period']
        
        # 手寫 MFI 公式
        tp = (df['high'] + df['low'] + df['close']) / 3
        mf = tp * df['volume']
        
        diff = tp.diff()
        positive_mf = mf.where(diff > 0, 0)
        negative_mf = mf.where(diff < 0, 0)
        
        pos_mf_sum = positive_mf.rolling(window=period).sum()
        neg_mf_sum = negative_mf.rolling(window=period).sum()
        
        mfr = pos_mf_sum / neg_mf_sum
        df['mfi'] = 100 - (100 / (1 + mfr))
        
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        if 'mfi' not in df.columns or df['mfi'].isna().all():
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "MFI 數據不足"
            )

        mfi = df['mfi'].iloc[-1]
        if pd.isna(mfi):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "MFI 數據不足"
            )

        details = {'mfi': mfi}

        if mfi < self.params['oversold']:
            ratio = (self.params['oversold'] - mfi) / self.params['oversold']
            score = self._scale_score(self.max_score * (0.6 + 0.4 * ratio))
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY if mfi < 10 else SignalType.BUY,
                score, mfi, details,
                f"MFI={mfi:.1f} 資金超賣（<{self.params['oversold']}），大量資金流出"
            )
        elif mfi > self.params['overbought']:
            ratio = (mfi - self.params['overbought']) / (100 - self.params['overbought'])
            score = self._scale_score(self.max_score * (0.6 + 0.4 * ratio))
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL if mfi > 90 else SignalType.SELL,
                score, mfi, details,
                f"MFI={mfi:.1f} 資金超買（>{self.params['overbought']}），大量資金流入"
            )
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, mfi, details,
                f"MFI={mfi:.1f} 資金流量正常"
            )
