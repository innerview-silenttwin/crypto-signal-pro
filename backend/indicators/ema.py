"""EMA Cross (指數移動平均線交叉) 插件"""

import pandas as pd
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('ema_cross')
class EMACrossIndicator(BaseIndicator):
    """
    EMA 交叉策略
    
    短期：EMA9/EMA21 交叉
    長期：EMA50/EMA200 交叉（黃金/死亡交叉）
    """

    def __init__(self, max_score: float = 15.0, params: dict = None):
        default_params = {
            'fast_period': 9,
            'slow_period': 21,
            'long_fast': 50,
            'long_slow': 200,
        }
        if params:
            default_params.update(params)
        super().__init__(name='EMA Cross', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        fp = self.params['fast_period']
        sp = self.params['slow_period']
        lf = self.params['long_fast']
        ls = self.params['long_slow']
        
        # 使用 pandas ewm 實作 EMA
        df[f'ema_{fp}'] = df['close'].ewm(span=fp, adjust=False).mean()
        df[f'ema_{sp}'] = df['close'].ewm(span=sp, adjust=False).mean()
        df[f'ema_{lf}'] = df['close'].ewm(span=lf, adjust=False).mean()
        df[f'ema_{ls}'] = df['close'].ewm(span=ls, adjust=False).mean()
        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        fp = self.params['fast_period']
        sp = self.params['slow_period']
        lf = self.params['long_fast']
        ls = self.params['long_slow']
        
        fast_col = f'ema_{fp}'
        slow_col = f'ema_{sp}'
        long_fast_col = f'ema_{lf}'
        long_slow_col = f'ema_{ls}'

        if not all(c in df.columns for c in [fast_col, slow_col]):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "EMA 數據不足"
            )

        if len(df) < 2:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "EMA 數據不足"
            )

        ema_fast = df[fast_col].iloc[-1]
        ema_slow = df[slow_col].iloc[-1]
        prev_ema_fast = df[fast_col].iloc[-2]
        prev_ema_slow = df[slow_col].iloc[-2]

        if any(pd.isna(v) for v in [ema_fast, ema_slow, prev_ema_fast, prev_ema_slow]):
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, 0, {}, "EMA 數據不足"
            )

        details = {
            f'ema_{fp}': ema_fast, f'ema_{sp}': ema_slow,
        }

        # 檢查長期 EMA 交叉
        has_long = long_fast_col in df.columns and long_slow_col in df.columns
        long_golden = False
        long_death = False
        
        if has_long and len(df) >= 2:
            lf_now = df[long_fast_col].iloc[-1]
            ls_now = df[long_slow_col].iloc[-1]
            lf_prev = df[long_fast_col].iloc[-2]
            ls_prev = df[long_slow_col].iloc[-2]
            if not any(pd.isna(v) for v in [lf_now, ls_now, lf_prev, ls_prev]):
                long_golden = lf_prev <= ls_prev and lf_now > ls_now
                long_death = lf_prev >= ls_prev and lf_now < ls_now
                details[f'ema_{lf}'] = lf_now
                details[f'ema_{ls}'] = ls_now

        # 短期黃金交叉
        golden_cross = prev_ema_fast <= prev_ema_slow and ema_fast > ema_slow
        # 短期死亡交叉
        death_cross = prev_ema_fast >= prev_ema_slow and ema_fast < ema_slow

        # 長期黃金交叉（權重更大）
        if long_golden:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, ema_fast, details,
                f"EMA{lf} 上穿 EMA{ls}（長期黃金交叉）→ 極強買入"
            )
        elif long_death:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, ema_fast, details,
                f"EMA{lf} 下穿 EMA{ls}（長期死亡交叉）→ 極強賣出"
            )
        elif golden_cross:
            score = self._scale_score(self.max_score * 0.75)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, ema_fast, details,
                f"EMA{fp} 上穿 EMA{sp}（短期黃金交叉）"
            )
        elif death_cross:
            score = self._scale_score(self.max_score * 0.75)
            return IndicatorSignal(
                self.name, SignalType.SELL, score, ema_fast, details,
                f"EMA{fp} 下穿 EMA{sp}（短期死亡交叉）"
            )
        # EMA 多頭排列
        elif ema_fast > ema_slow:
            gap_pct = (ema_fast - ema_slow) / ema_slow * 100
            score = self._scale_score(self.max_score * min(0.4, gap_pct / 5))
            return IndicatorSignal(
                self.name, SignalType.BUY, score, ema_fast, details,
                f"EMA 多頭排列（快線高於慢線 {gap_pct:.2f}%）"
            )
        elif ema_fast < ema_slow:
            gap_pct = (ema_slow - ema_fast) / ema_slow * 100
            score = self._scale_score(self.max_score * min(0.4, gap_pct / 5))
            return IndicatorSignal(
                self.name, SignalType.SELL, score, ema_fast, details,
                f"EMA 空頭排列（快線低於慢線 {gap_pct:.2f}%）"
            )
        else:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, ema_fast, details,
                "EMA 無明顯交叉信號"
            )
