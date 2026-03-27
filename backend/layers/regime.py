"""
盤勢辨識層 (Market Regime Detection Layer)

融合技術分析概念：
1. 趨勢確認：頭頭高/底底高 (Swing High/Low)
2. 均線排列：5/10/20/60MA 多頭排列 + 方向判斷
3. 位階偵測：底部起漲 vs 高檔區
4. K 線型態：長紅/長黑/長上影/吞噬
5. 量價分析：底部爆量、高檔爆量不漲
6. ATR/ADX 趨勢強度

產出盤勢狀態：STRONG_BULL / BULL / CONSOLIDATION / BEAR / REVERSAL_TOP / REVERSAL_BOTTOM
根據狀態調整買賣分數乘數。
"""

import numpy as np
import pandas as pd
from .base import BaseLayer, LayerModifier, LayerRegistry


class RegimeLayer(BaseLayer):
    """盤勢辨識層"""

    # 盤勢類型
    STRONG_BULL = "強勢多頭"       # 均線多排 + 趨勢確認 + 量增
    BULL = "多頭"                  # 趨勢向上但未完全展開
    CONSOLIDATION = "盤整"         # 無明確方向
    BEAR = "空頭"                  # 趨勢向下
    REVERSAL_TOP = "高檔轉折"     # 頭部訊號
    REVERSAL_BOTTOM = "底部轉強"   # 底部訊號

    def __init__(self, enabled: bool = True, **kwargs):
        super().__init__("regime", enabled)

    def compute_modifier(self, symbol: str, df: pd.DataFrame,
                         sector_id: str = "") -> LayerModifier:
        if not self.enabled or len(df) < 120:
            return LayerModifier(layer_name=self.name, active=False,
                                 reason="數據不足或未啟用")

        result = LayerModifier(layer_name=self.name)

        # ── 計算所有子信號 ──
        trend = self._detect_trend(df)
        ma_info = self._detect_ma_alignment(df)
        position = self._detect_position(df)
        kline = self._detect_kline_pattern(df)
        volume = self._detect_volume_pattern(df, position)
        adx_info = self._detect_adx_regime(df)

        # ── 綜合判斷盤勢 ──
        regime, confidence = self._classify_regime(
            trend, ma_info, position, kline, volume, adx_info
        )

        # ── 根據盤勢設定修正值 ──
        result.regime = regime
        result.details = {
            "trend": trend,
            "ma_alignment": ma_info,
            "position": position,
            "kline_pattern": kline,
            "volume_pattern": volume,
            "adx": adx_info,
            "confidence": confidence,
        }

        if regime == self.STRONG_BULL:
            result.buy_multiplier = 1.3
            result.sell_multiplier = 0.5
            result.reason = "強勢多頭：均線多排+趨勢確認+量增，加強買入信號"

        elif regime == self.BULL:
            result.buy_multiplier = 1.15
            result.sell_multiplier = 0.7
            result.reason = "多頭趨勢中，適度加強買入"

        elif regime == self.CONSOLIDATION:
            result.buy_multiplier = 0.6
            result.sell_multiplier = 0.6
            result.reason = "盤整期：降低所有信號，避免追高殺低"

        elif regime == self.BEAR:
            result.buy_multiplier = 0.3
            result.sell_multiplier = 1.3
            result.veto_buy = True
            result.reason = "空頭趨勢：否決買入，加強賣出"

        elif regime == self.REVERSAL_TOP:
            result.buy_multiplier = 0.2
            result.sell_multiplier = 1.5
            result.veto_buy = True
            result.reason = "高檔轉折：" + kline.get("reason", "頭部警告")

        elif regime == self.REVERSAL_BOTTOM:
            result.buy_multiplier = 1.4
            result.sell_multiplier = 0.3
            result.buy_offset = 10.0
            result.reason = "底部轉強：" + kline.get("reason", "底部訊號")

        return result

    # ════════════════════════════════════════════════════════════
    # 1. 趨勢確認：頭頭高/底底高
    # ════════════════════════════════════════════════════════════

    def _detect_trend(self, df: pd.DataFrame) -> dict:
        """偵測 swing high/low 判斷趨勢方向"""
        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        n = len(closes)

        # 找近期 swing points (用 5 根 K 棒)
        swing_highs = []
        swing_lows = []
        window = 5

        for i in range(window, n - window):
            if highs[i] == max(highs[i - window:i + window + 1]):
                swing_highs.append((i, highs[i]))
            if lows[i] == min(lows[i - window:i + window + 1]):
                swing_lows.append((i, lows[i]))

        # 取最近 3 個 swing points
        recent_highs = swing_highs[-3:] if len(swing_highs) >= 3 else swing_highs
        recent_lows = swing_lows[-3:] if len(swing_lows) >= 3 else swing_lows

        higher_highs = False
        higher_lows = False
        lower_highs = False
        lower_lows = False

        if len(recent_highs) >= 2:
            higher_highs = all(
                recent_highs[i][1] > recent_highs[i - 1][1]
                for i in range(1, len(recent_highs))
            )
            lower_highs = all(
                recent_highs[i][1] < recent_highs[i - 1][1]
                for i in range(1, len(recent_highs))
            )

        if len(recent_lows) >= 2:
            higher_lows = all(
                recent_lows[i][1] > recent_lows[i - 1][1]
                for i in range(1, len(recent_lows))
            )
            lower_lows = all(
                recent_lows[i][1] < recent_lows[i - 1][1]
                for i in range(1, len(recent_lows))
            )

        if higher_highs and higher_lows:
            direction = "BULL"
        elif lower_highs and lower_lows:
            direction = "BEAR"
        else:
            direction = "NEUTRAL"

        return {
            "direction": direction,
            "higher_highs": higher_highs,
            "higher_lows": higher_lows,
            "lower_highs": lower_highs,
            "lower_lows": lower_lows,
        }

    # ════════════════════════════════════════════════════════════
    # 2. 均線排列（六六大順核心）
    # ════════════════════════════════════════════════════════════

    def _detect_ma_alignment(self, df: pd.DataFrame) -> dict:
        """5/10/20/60MA 排列判斷"""
        closes = df["close"]

        ma5 = closes.rolling(5).mean()
        ma10 = closes.rolling(10).mean()
        ma20 = closes.rolling(20).mean()
        ma60 = closes.rolling(60).mean()

        last = len(df) - 1
        prev5 = max(0, last - 5)

        cur_ma5 = ma5.iloc[last]
        cur_ma10 = ma10.iloc[last]
        cur_ma20 = ma20.iloc[last]
        cur_ma60 = ma60.iloc[last]
        cur_close = closes.iloc[last]

        # 多頭排列：close > 5MA > 10MA > 20MA > 60MA
        bull_alignment = (
            cur_close > cur_ma5 > cur_ma10 > cur_ma20 > cur_ma60
        )

        # 空頭排列
        bear_alignment = (
            cur_close < cur_ma5 < cur_ma10 < cur_ma20 < cur_ma60
        )

        # 均線方向（5日前 vs 現在）
        ma20_up = cur_ma20 > ma20.iloc[prev5] if prev5 < last else False
        ma60_up = cur_ma60 > ma60.iloc[prev5] if prev5 < last else False

        # 股價相對位置
        above_ma5 = cur_close > cur_ma5
        above_ma20 = cur_close > cur_ma20

        # 六六大順分數 (0-6)
        score = sum([
            bull_alignment,                # 多頭排列
            above_ma5,                     # 股價在5MA之上
            above_ma20,                    # 股價在20MA之上
            ma20_up,                       # 20MA 方向向上
            ma60_up,                       # 60MA 方向向上
            cur_close > cur_ma60,          # 股價在60MA之上
        ])

        return {
            "bull_alignment": bull_alignment,
            "bear_alignment": bear_alignment,
            "ma20_up": ma20_up,
            "ma60_up": ma60_up,
            "above_ma5": above_ma5,
            "above_ma20": above_ma20,
            "score": score,  # 0-6
        }

    # ════════════════════════════════════════════════════════════
    # 3. 位階偵測（底部起漲 vs 高檔）
    # ════════════════════════════════════════════════════════════

    def _detect_position(self, df: pd.DataFrame) -> dict:
        """判斷股價在近120日的相對位置"""
        closes = df["close"].values
        lookback = min(120, len(closes))
        recent = closes[-lookback:]

        high_120 = np.max(recent)
        low_120 = np.min(recent)
        price_range = high_120 - low_120

        if price_range == 0:
            pct = 50.0
        else:
            pct = (closes[-1] - low_120) / price_range * 100

        # 位階分類
        if pct >= 85:
            zone = "HIGH"       # 高檔區
        elif pct >= 60:
            zone = "MID_HIGH"   # 中高檔
        elif pct >= 40:
            zone = "MID"        # 中間
        elif pct >= 15:
            zone = "MID_LOW"    # 中低檔
        else:
            zone = "LOW"        # 低檔區

        return {
            "percentile": round(pct, 1),
            "zone": zone,
            "high_120": round(high_120, 2),
            "low_120": round(low_120, 2),
        }

    # ════════════════════════════════════════════════════════════
    # 4. K 線型態辨識
    # ════════════════════════════════════════════════════════════

    def _detect_kline_pattern(self, df: pd.DataFrame) -> dict:
        """偵測關鍵 K 線型態"""
        if len(df) < 3:
            return {"pattern": "NONE", "reason": ""}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        o, h, l, c = last["open"], last["high"], last["low"], last["close"]
        body = abs(c - o)
        full_range = h - l
        upper_shadow = h - max(o, c)
        lower_shadow = min(o, c) - l

        patterns = []
        reason_parts = []

        if full_range == 0:
            return {"pattern": "NONE", "reason": "無波動"}

        body_ratio = body / full_range

        # ── 長紅棒（底部起漲訊號）──
        if c > o and body_ratio > 0.7 and body > 0:
            avg_body = df["close"].tail(20).diff().abs().mean()
            if body > avg_body * 1.5:
                patterns.append("LONG_RED_BAR")
                reason_parts.append("長紅K棒（多頭力道強勁）")

        # ── 長黑棒 ──
        if o > c and body_ratio > 0.7 and body > 0:
            avg_body = df["close"].tail(20).diff().abs().mean()
            if body > avg_body * 1.5:
                patterns.append("LONG_BLACK_BAR")
                reason_parts.append("長黑K棒（空頭力道強勁）")

        # ── 長上影線（高檔賣壓沉重）──
        if full_range > 0 and upper_shadow / full_range > 0.5 and body_ratio < 0.3:
            patterns.append("LONG_UPPER_SHADOW")
            reason_parts.append("長上影線（賣壓沉重）")

        # ── 長下影線（低檔買盤支撐）──
        if full_range > 0 and lower_shadow / full_range > 0.5 and body_ratio < 0.3:
            patterns.append("LONG_LOWER_SHADOW")
            reason_parts.append("長下影線（買盤支撐）")

        # ── 長黑吞噬（主力出貨）──
        prev_o, prev_c = prev["open"], prev["close"]
        if (prev_c > prev_o and  # 前一根紅K
            o > c and            # 當根黑K
            o >= prev_c and      # 開盤 >= 前收盤
            c <= prev_o):        # 收盤 <= 前開盤
            patterns.append("BEARISH_ENGULFING")
            reason_parts.append("長黑吞噬（主力出貨訊號）")

        # ── 多頭吞噬（底部反轉）──
        if (prev_o > prev_c and  # 前一根黑K
            c > o and            # 當根紅K
            o <= prev_c and      # 開盤 <= 前收盤
            c >= prev_o):        # 收盤 >= 前開盤
            patterns.append("BULLISH_ENGULFING")
            reason_parts.append("多頭吞噬（底部反轉訊號）")

        # ── 十字星（猶豫不決）──
        if body_ratio < 0.1 and full_range > 0:
            patterns.append("DOJI")
            reason_parts.append("十字星（多空僵持）")

        primary = patterns[0] if patterns else "NONE"
        return {
            "pattern": primary,
            "all_patterns": patterns,
            "reason": "、".join(reason_parts) if reason_parts else "",
            "body_ratio": round(body_ratio, 3),
            "upper_shadow_ratio": round(upper_shadow / full_range, 3) if full_range > 0 else 0,
        }

    # ════════════════════════════════════════════════════════════
    # 5. 量價分析
    # ════════════════════════════════════════════════════════════

    def _detect_volume_pattern(self, df: pd.DataFrame, position: dict) -> dict:
        """成交量型態 + 位階結合判斷"""
        if "volume" not in df.columns or len(df) < 20:
            return {"pattern": "NORMAL", "reason": ""}

        vol = df["volume"].values
        cur_vol = vol[-1]
        avg_vol_20 = np.mean(vol[-20:])

        if avg_vol_20 == 0:
            return {"pattern": "NORMAL", "reason": "", "vol_ratio": 0}

        vol_ratio = cur_vol / avg_vol_20

        # 今日漲跌幅
        change_pct = (df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2] * 100

        zone = position.get("zone", "MID")
        pattern = "NORMAL"
        reason = ""

        # ── 底部爆量 + 長紅 = 主力進場 ──
        if zone in ("LOW", "MID_LOW") and vol_ratio > 2.0 and change_pct > 2:
            pattern = "BOTTOM_VOLUME_SURGE"
            reason = f"底部爆量長紅（量比:{vol_ratio:.1f}x, 漲{change_pct:.1f}%）→ 主力進場訊號"

        # ── 高檔爆量不漲 = 主力出貨 ──
        elif zone in ("HIGH", "MID_HIGH") and vol_ratio > 2.0 and change_pct < 1:
            pattern = "TOP_VOLUME_NO_RISE"
            reason = f"高檔爆量不漲（量比:{vol_ratio:.1f}x, 漲{change_pct:.1f}%）→ 主力出貨警告"

        # ── 高檔爆量下跌 ──
        elif zone in ("HIGH", "MID_HIGH") and vol_ratio > 1.5 and change_pct < -2:
            pattern = "TOP_VOLUME_DROP"
            reason = f"高檔爆量下殺（量比:{vol_ratio:.1f}x, 跌{change_pct:.1f}%）→ 趨勢反轉"

        # ── 溫和放量上漲 = 健康趨勢 ──
        elif vol_ratio > 1.2 and change_pct > 1:
            pattern = "HEALTHY_VOLUME_UP"
            reason = f"量增價漲（量比:{vol_ratio:.1f}x）→ 趨勢健康"

        # ── 縮量 = 觀望 ──
        elif vol_ratio < 0.5:
            pattern = "LOW_VOLUME"
            reason = "極度縮量 → 市場觀望"

        return {
            "pattern": pattern,
            "reason": reason,
            "vol_ratio": round(vol_ratio, 2),
            "change_pct": round(change_pct, 2),
        }

    # ════════════════════════════════════════════════════════════
    # 6. ADX 趨勢強度
    # ════════════════════════════════════════════════════════════

    def _detect_adx_regime(self, df: pd.DataFrame) -> dict:
        """用 ADX 判斷趨勢強弱"""
        if "adx" not in df.columns:
            return {"adx": 0, "trending": False, "adx_rising": False}

        adx_vals = df["adx"].dropna()
        if len(adx_vals) < 5:
            return {"adx": 0, "trending": False, "adx_rising": False}

        cur_adx = adx_vals.iloc[-1]
        prev_adx = adx_vals.iloc[-5]

        return {
            "adx": round(cur_adx, 1),
            "trending": cur_adx > 25,
            "strong_trend": cur_adx > 40,
            "adx_rising": cur_adx > prev_adx,
        }

    # ════════════════════════════════════════════════════════════
    # 綜合判斷
    # ════════════════════════════════════════════════════════════

    def _classify_regime(self, trend, ma_info, position, kline, volume, adx_info) -> tuple:
        """
        綜合所有子信號，判斷最終盤勢狀態

        Returns: (regime_name, confidence_0_100)
        """
        score = 0       # 正分 = 偏多, 負分 = 偏空
        max_score = 0

        zone = position["zone"]
        vol_pattern = volume["pattern"]
        kline_pattern = kline["pattern"]

        # ── 高檔轉折偵測（優先判斷）──
        if zone in ("HIGH", "MID_HIGH"):
            top_signals = 0
            if kline_pattern in ("LONG_UPPER_SHADOW", "BEARISH_ENGULFING", "LONG_BLACK_BAR"):
                top_signals += 2
            if vol_pattern in ("TOP_VOLUME_NO_RISE", "TOP_VOLUME_DROP"):
                top_signals += 2
            if not ma_info["above_ma5"]:
                top_signals += 1
            if adx_info.get("adx_rising") is False and adx_info.get("trending"):
                top_signals += 1

            if top_signals >= 3:
                return (self.REVERSAL_TOP, min(top_signals * 20, 90))

        # ── 底部轉強偵測 ──
        if zone in ("LOW", "MID_LOW"):
            bottom_signals = 0
            if kline_pattern in ("LONG_RED_BAR", "BULLISH_ENGULFING", "LONG_LOWER_SHADOW"):
                bottom_signals += 2
            if vol_pattern == "BOTTOM_VOLUME_SURGE":
                bottom_signals += 2
            if ma_info["above_ma5"]:
                bottom_signals += 1
            if adx_info.get("adx_rising"):
                bottom_signals += 1

            if bottom_signals >= 3:
                return (self.REVERSAL_BOTTOM, min(bottom_signals * 20, 90))

        # ── 趨勢判斷 ──
        # 趨勢確認 (+/-3)
        if trend["direction"] == "BULL":
            score += 3
        elif trend["direction"] == "BEAR":
            score -= 3
        max_score += 3

        # 均線排列 (+/-3)
        if ma_info["bull_alignment"]:
            score += 3
        elif ma_info["bear_alignment"]:
            score -= 3
        elif ma_info["score"] >= 4:
            score += 1
        elif ma_info["score"] <= 2:
            score -= 1
        max_score += 3

        # 均線方向 (+/-2)
        if ma_info["ma20_up"] and ma_info["ma60_up"]:
            score += 2
        elif not ma_info["ma20_up"] and not ma_info["ma60_up"]:
            score -= 2
        max_score += 2

        # ADX 趨勢強度 (+/-1)
        if adx_info.get("trending") and adx_info.get("adx_rising"):
            score += 1 if score > 0 else -1  # 加強當前方向
        max_score += 1

        # 量價 (+/-1)
        if vol_pattern == "HEALTHY_VOLUME_UP":
            score += 1
        elif vol_pattern in ("TOP_VOLUME_NO_RISE", "TOP_VOLUME_DROP"):
            score -= 1
        max_score += 1

        # ── 分類 ──
        confidence = abs(score) / max_score * 100 if max_score > 0 else 0

        if score >= 7:
            return (self.STRONG_BULL, confidence)
        elif score >= 3:
            return (self.BULL, confidence)
        elif score <= -5:
            return (self.BEAR, confidence)
        else:
            return (self.CONSOLIDATION, confidence)


# 註冊到 LayerRegistry
LayerRegistry.register("regime", RegimeLayer)
