"""Pullback Support - 拉回均線支撐買點偵測，含破底保護"""

import pandas as pd
import numpy as np
from .base import BaseIndicator, IndicatorSignal, SignalType
from .registry import register_indicator


@register_indicator('pullback_support')
class PullbackSupportIndicator(BaseIndicator):
    """
    拉回均線支撐買點偵測

    核心邏輯（順序）：
    1. 破底保護：收盤跌破 EMA200 一定幅度 → 強烈賣出，不進場
    2. 趨勢確認：收盤必須在 EMA200 之上
    3. RSI 護城河：RSI 不得低於門檻（避開技術面崩潰）
    4. 拉回到 EMA21：最佳低風險買點（縮量更佳）
    5. 拉回到 EMA50：較深拉回，需 RSI > 40 + 縮量確認

    適用場景：主升段中的波段回調買點，而非底部反轉。
    """

    def __init__(self, max_score: float = 15.0, params: dict = None):
        default_params = {
            'ema_fast': 21,
            'ema_mid': 50,
            'ema_slow': 200,
            'near_pct': 0.025,          # 距 EMA 幾 % 內算「接近支撐」
            'rsi_period': 14,
            'rsi_floor': 35,            # RSI 最低門檻（低於此視為破底）
            'rsi_deep_floor': 40,       # EMA50 拉回需要更高的 RSI
            'vol_ma_short': 5,          # 短期量能均線（判斷縮量）
            'vol_ma_long': 20,          # 長期量能均線
            'vol_shrink_ratio': 0.80,   # 量能 ≤ 長均線的此比例算縮量
            'breakdown_pct': 0.03,      # 跌破 EMA200 幾 % 啟動破底警示
        }
        if params:
            default_params.update(params)
        super().__init__(name='Pullback Support', max_score=max_score, params=default_params)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        ef = self.params['ema_fast']
        em = self.params['ema_mid']
        es = self.params['ema_slow']

        df[f'ps_ema{ef}'] = df['close'].ewm(span=ef, adjust=False).mean()
        df[f'ps_ema{em}'] = df['close'].ewm(span=em, adjust=False).mean()
        df[f'ps_ema{es}'] = df['close'].ewm(span=es, adjust=False).mean()

        # RSI
        rp = self.params['rsi_period']
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        avg_gain = gain.rolling(window=rp, min_periods=rp).mean()
        avg_loss = loss.rolling(window=rp, min_periods=rp).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df['ps_rsi'] = 100 - (100 / (1 + rs))

        # 短 vs. 長均量（判斷縮量）
        vs = self.params['vol_ma_short']
        vl = self.params['vol_ma_long']
        df['ps_vol_short'] = df['volume'].rolling(window=vs).mean()
        df['ps_vol_long'] = df['volume'].rolling(window=vl).mean()

        return df

    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        ef = self.params['ema_fast']
        em = self.params['ema_mid']
        es = self.params['ema_slow']
        required = [f'ps_ema{ef}', f'ps_ema{em}', f'ps_ema{es}', 'ps_rsi']

        if not all(c in df.columns for c in required):
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "Pullback Support 數據不足")

        close = df['close'].iloc[-1]
        ema_f = df[f'ps_ema{ef}'].iloc[-1]
        ema_m = df[f'ps_ema{em}'].iloc[-1]
        ema_s = df[f'ps_ema{es}'].iloc[-1]
        rsi = df['ps_rsi'].iloc[-1]

        if any(pd.isna(v) for v in [close, ema_f, ema_m, ema_s, rsi]):
            return IndicatorSignal(self.name, SignalType.NEUTRAL, 0, 0, {}, "Pullback Support 數據不足")

        # 縮量判斷
        vol_shrink = False
        if 'ps_vol_short' in df.columns and 'ps_vol_long' in df.columns:
            vs_val = df['ps_vol_short'].iloc[-1]
            vl_val = df['ps_vol_long'].iloc[-1]
            if not pd.isna(vs_val) and not pd.isna(vl_val) and vl_val > 0:
                vol_shrink = vs_val <= vl_val * self.params['vol_shrink_ratio']

        details = {
            f'ema{ef}': round(ema_f, 2), f'ema{em}': round(ema_m, 2),
            f'ema{es}': round(ema_s, 2), 'rsi': round(rsi, 1),
            'vol_shrink': bool(vol_shrink),
        }

        breakdown_pct = self.params['breakdown_pct']

        # ── 1. 破底保護（最高優先）──────────────────────────────────
        if close < ema_s * (1 - breakdown_pct):
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_SELL, score, close, details,
                f"跌破EMA{es}（{breakdown_pct*100:.0f}%以下）= 趨勢破壞 → 禁止做多"
            )

        # ── 2. 趨勢未確立 ────────────────────────────────────────────
        if close < ema_s:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, close, details,
                f"收盤低於EMA{es}，多頭趨勢未確立，等待"
            )

        # ── 3. RSI 過低護城河 ────────────────────────────────────────
        if rsi < self.params['rsi_floor']:
            return IndicatorSignal(
                self.name, SignalType.NEUTRAL, 0, close, details,
                f"RSI={rsi:.0f} 低於門檻{self.params['rsi_floor']}，拉回可能轉為破底，不進場"
            )

        near = self.params['near_pct']
        at_ema_f = abs(close - ema_f) / ema_f <= near and close >= ema_f * (1 - near)
        at_ema_m = abs(close - ema_m) / ema_m <= near and close >= ema_m * (1 - near)

        # ── 4. 拉回到 EMA21（縮量）──────────────────────────────────
        if at_ema_f and vol_shrink:
            score = self._scale_score(self.max_score)
            return IndicatorSignal(
                self.name, SignalType.STRONG_BUY, score, close, details,
                f"拉回EMA{ef}支撐+縮量（RSI={rsi:.0f}）→ 低風險波段買點"
            )
        # ── 5. 拉回到 EMA21（量能正常）──────────────────────────────
        if at_ema_f:
            score = self._scale_score(self.max_score * 0.72)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, close, details,
                f"拉回EMA{ef}支撐（RSI={rsi:.0f}）→ 趨勢拉回買點"
            )
        # ── 6. 拉回到 EMA50 + 縮量 + RSI 足夠 ───────────────────────
        if at_ema_m and vol_shrink and rsi >= self.params['rsi_deep_floor']:
            score = self._scale_score(self.max_score * 0.85)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, close, details,
                f"拉回EMA{em}支撐+縮量（RSI={rsi:.0f}）→ 較深拉回，可考慮進場"
            )
        # ── 7. 拉回到 EMA50（RSI 足夠）──────────────────────────────
        if at_ema_m and rsi >= self.params['rsi_deep_floor']:
            score = self._scale_score(self.max_score * 0.55)
            return IndicatorSignal(
                self.name, SignalType.BUY, score, close, details,
                f"拉回EMA{em}支撐（RSI={rsi:.0f}），量能偏大，謹慎"
            )

        return IndicatorSignal(
            self.name, SignalType.NEUTRAL, 0, close, details,
            f"無拉回買點（EMA{ef}={ema_f:.1f}，EMA{em}={ema_m:.1f}，RSI={rsi:.0f}）"
        )
