"""
加密貨幣資金流向層 (Crypto Flow Layer)

結合兩個免費、有長期歷史的情緒/資金指標：
1. Fear & Greed Index (alternative.me) — 每日，2018 年起
2. Binance Funding Rate — 每 8 小時，2019/9 起

策略邏輯（逆向思維）：
- 極度恐懼 + 資金費率偏低 → 加強買入（市場過度悲觀）
- 極度貪婪 + 資金費率偏高 → 加強賣出（市場過度樂觀）
- 中間區域 → 不做修正，交給技術面判斷
"""

import os
import pandas as pd
import numpy as np
from .base import BaseLayer, LayerModifier, LayerRegistry


class CryptoFlowLayer(BaseLayer):
    """加密貨幣資金流向分析層"""

    def __init__(self, enabled: bool = True, data_dir: str = None, **kwargs):
        super().__init__("crypto_flow", enabled)
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data"
        )
        self._fng_df = None
        self._fr_df = None

    def _load_data(self):
        """懶加載歷史資料"""
        if self._fng_df is None:
            fng_path = os.path.join(self.data_dir, "btc_fear_greed.csv")
            if os.path.exists(fng_path):
                self._fng_df = pd.read_csv(fng_path, index_col="timestamp", parse_dates=True)

        if self._fr_df is None:
            fr_path = os.path.join(self.data_dir, "btc_funding_rate.csv")
            if os.path.exists(fr_path):
                self._fr_df = pd.read_csv(fr_path, index_col="timestamp", parse_dates=True)
                # 計算 8 小時 / 日均資金費率
                self._fr_daily = self._fr_df.resample("1D").mean()

    def _get_fng(self, dt: pd.Timestamp) -> float:
        """取得指定日期的 Fear & Greed 值 (0-100)"""
        if self._fng_df is None:
            return 50.0  # 無資料時回傳中性值
        # 找到 <= dt 的最近一筆
        mask = self._fng_df.index <= dt
        if mask.any():
            return float(self._fng_df.loc[mask, "fng_value"].iloc[-1])
        return 50.0

    def _get_funding_rate(self, dt: pd.Timestamp) -> float:
        """取得指定日期附近的日均資金費率"""
        if self._fr_daily is None:
            return 0.0001  # 預設中性值
        mask = self._fr_daily.index <= dt
        if mask.any():
            return float(self._fr_daily.loc[mask, "funding_rate"].iloc[-1])
        return 0.0001

    def _get_funding_rate_percentile(self, dt: pd.Timestamp, lookback_days: int = 90) -> float:
        """計算資金費率在近 N 日的百分位"""
        if self._fr_daily is None:
            return 50.0
        mask = self._fr_daily.index <= dt
        recent = self._fr_daily.loc[mask].tail(lookback_days)
        if len(recent) < 10:
            return 50.0
        current = recent["funding_rate"].iloc[-1]
        pct = (recent["funding_rate"] < current).sum() / len(recent) * 100
        return pct

    def compute_modifier(self, symbol: str, df: pd.DataFrame,
                         sector_id: str = "") -> LayerModifier:
        if not self.enabled:
            return LayerModifier(layer_name=self.name, active=False)

        self._load_data()

        current_time = df.index[-1] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now()

        fng = self._get_fng(current_time)
        fr = self._get_funding_rate(current_time)
        fr_pct = self._get_funding_rate_percentile(current_time)

        result = LayerModifier(layer_name=self.name)
        result.details = {
            "fear_greed": fng,
            "funding_rate": round(fr * 100, 4),  # 轉 %
            "funding_rate_pct": round(fr_pct, 1),
        }

        # === 綜合評分 ===
        # Fear & Greed 分數 (-2 ~ +2)
        fng_score = 0
        if fng <= 15:
            fng_score = 2       # 極度恐懼 → 強烈看多
            fng_label = "極度恐懼"
        elif fng <= 30:
            fng_score = 1       # 恐懼 → 偏多
            fng_label = "恐懼"
        elif fng >= 85:
            fng_score = -2      # 極度貪婪 → 強烈看空
            fng_label = "極度貪婪"
        elif fng >= 70:
            fng_score = -1      # 貪婪 → 偏空
            fng_label = "貪婪"
        else:
            fng_score = 0       # 中性
            fng_label = "中性"

        # 資金費率分數 (-2 ~ +2)
        fr_score = 0
        if fr_pct >= 90:
            fr_score = -2       # 費率極高 → 多方過度槓桿
            fr_label = "極高費率"
        elif fr_pct >= 75:
            fr_score = -1       # 費率偏高
            fr_label = "偏高費率"
        elif fr_pct <= 10:
            fr_score = 2        # 費率極低 → 空方過度
            fr_label = "極低費率"
        elif fr_pct <= 25:
            fr_score = 1        # 費率偏低
            fr_label = "偏低費率"
        else:
            fr_score = 0
            fr_label = "中性費率"

        total_score = fng_score + fr_score  # -4 ~ +4

        # === 映射到修正值 ===
        if total_score >= 3:
            # 極度恐懼 + 低費率 → 強烈加強買入
            result.buy_multiplier = 1.4
            result.sell_multiplier = 0.4
            result.buy_offset = 8.0
            result.reason = f"市場極度恐懼（FnG={fng:.0f} {fng_label}, {fr_label}）→ 逆向加碼買入"
        elif total_score == 2:
            result.buy_multiplier = 1.25
            result.sell_multiplier = 0.6
            result.buy_offset = 5.0
            result.reason = f"市場恐懼（FnG={fng:.0f} {fng_label}, {fr_label}）→ 加強買入"
        elif total_score == 1:
            result.buy_multiplier = 1.1
            result.sell_multiplier = 0.85
            result.reason = f"市場偏悲觀（FnG={fng:.0f}, {fr_label}）→ 略加強買入"
        elif total_score == -1:
            result.buy_multiplier = 0.85
            result.sell_multiplier = 1.1
            result.reason = f"市場偏樂觀（FnG={fng:.0f}, {fr_label}）→ 略加強賣出"
        elif total_score == -2:
            result.buy_multiplier = 0.6
            result.sell_multiplier = 1.25
            result.sell_offset = 5.0
            result.reason = f"市場貪婪（FnG={fng:.0f} {fng_label}, {fr_label}）→ 加強賣出"
        elif total_score <= -3:
            result.buy_multiplier = 0.4
            result.sell_multiplier = 1.4
            result.sell_offset = 8.0
            result.veto_buy = True
            result.reason = f"市場極度貪婪（FnG={fng:.0f} {fng_label}, {fr_label}）→ 否決買入"
        else:
            # total_score == 0 → 中性，不修正
            result.active = False
            result.reason = f"市場情緒中性（FnG={fng:.0f}, {fr_label}）→ 不修正"

        result.regime = fng_label
        return result


# 註冊到 LayerRegistry
LayerRegistry.register("crypto_flow", CryptoFlowLayer)
