"""
5 個 SELL 策略。每個策略接收 (row, pos, taiex_row) → (do_sell: bool, reason: str)。

row:  本日訊號快取的一個 pd.Series（含 close, atr14, ma10/20/50/200, ema21, macd, ...）
pos:  Position 物件（含 entry_price, highest_since_entry, atr14_at_entry 等）
taiex_row: 同日 ^TWII 的 row（含 close, ma200, bull, bear）

通用守則（在 evaluator 中外加，不在策略內處理）：
- stop_loss% / take_profit% 是「保命防呆」，五個策略都會套用（包含 baseline）
- 標準綜合 SELL 信號（sell_score >= 門檻 且 > buy_score）也是五策略共用基準

策略本身只決定「主動出場」的時機差異。
"""

from dataclasses import dataclass
from typing import Tuple

import pandas as pd


@dataclass
class Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: float
    cost: float
    highest_since_entry: float = 0.0
    atr14_at_entry: float = 0.0


def _trend_break_s8(row: pd.Series, atr_mult: float = 3.0) -> bool:
    """從 20 日高點下跌 ≥ atr_mult × ATR14（S8）"""
    h20 = row.get('high20d')
    atr = row.get('atr14')
    c = row.get('close')
    if pd.isna(h20) or pd.isna(atr) or pd.isna(c) or atr <= 0:
        return False
    return (h20 - c) >= atr_mult * atr


def _ma20_break_red3(row: pd.Series) -> bool:
    """S9: 連 3 黑 + 收 < MA20"""
    c = row.get('close')
    ma20 = row.get('ma20')
    if pd.isna(c) or pd.isna(ma20):
        return False
    return bool(row.get('red3', False)) and c < ma20


def _regime_tag(taiex_row) -> str:
    if taiex_row is None or len(taiex_row) == 0:
        return "unknown"
    if bool(taiex_row.get('bear', False)):
        return "bear"
    if bool(taiex_row.get('bull', False)):
        return "bull"
    return "neutral"


# ─────────────────────────────────────────────────────────────────
# S0: Baseline — 現行 production 邏輯
#   S8 (3×ATR) + S9 + 標準信號（後者由 evaluator 處理）
# ─────────────────────────────────────────────────────────────────
def baseline(row, pos, taiex_row) -> Tuple[bool, str]:
    if _trend_break_s8(row, 3.0):
        return True, "S8_3xATR"
    if _ma20_break_red3(row):
        return True, "S9_red3+MA20"
    return False, ""


# ─────────────────────────────────────────────────────────────────
# S1: Trailing stop ATR — 從持倉以來最高價回落 3×ATR 即賣
#   主動「鎖利」，不等 20 日高點
# ─────────────────────────────────────────────────────────────────
def trailing_atr(row, pos, taiex_row) -> Tuple[bool, str]:
    c = row.get('close')
    atr = row.get('atr14')
    if pd.isna(c) or pd.isna(atr) or atr <= 0 or pos.highest_since_entry <= 0:
        return False, ""
    if (pos.highest_since_entry - c) >= 3.0 * atr:
        return True, f"trailing_3xATR(from{pos.highest_since_entry:.1f})"
    return False, ""


# ─────────────────────────────────────────────────────────────────
# S2: MA break — 跌破 EMA21（中期趨勢線）
# ─────────────────────────────────────────────────────────────────
def ma_break(row, pos, taiex_row) -> Tuple[bool, str]:
    c = row.get('close')
    ema21 = row.get('ema21')
    if pd.isna(c) or pd.isna(ema21):
        return False, ""
    if c < ema21:
        return True, "close<EMA21"
    return False, ""


# ─────────────────────────────────────────────────────────────────
# S3: Adaptive trend_break — 根據 TAIEX regime 動態調整 ATR 門檻
#   - bear 時用 1.5×ATR（更敏感）
#   - neutral 用 2.5×ATR
#   - bull 維持 3.0×ATR（不被假摔洗）
# ─────────────────────────────────────────────────────────────────
def adaptive_trend_break(row, pos, taiex_row) -> Tuple[bool, str]:
    tag = _regime_tag(taiex_row)
    if tag == "bear":
        mult = 1.5
    elif tag == "bull":
        mult = 3.0
    else:
        mult = 2.5
    if _trend_break_s8(row, mult):
        return True, f"adaptive_S8_{mult}xATR({tag})"
    if _ma20_break_red3(row):
        return True, "S9_red3+MA20"
    return False, ""


# ─────────────────────────────────────────────────────────────────
# S4: Regime-aware combo — TAIEX bear 時開啟 trailing 8% + EMA21 break
#                          TAIEX bull 時維持 baseline
# ─────────────────────────────────────────────────────────────────
def regime_combo(row, pos, taiex_row) -> Tuple[bool, str]:
    tag = _regime_tag(taiex_row)

    if tag == "bear":
        # 啟用 trailing + EMA21 break
        c = row.get('close')
        ema21 = row.get('ema21')
        atr = row.get('atr14')
        if pd.notna(c) and pd.notna(ema21) and c < ema21:
            return True, "bear:close<EMA21"
        if (pd.notna(c) and pd.notna(atr) and atr > 0
                and pos.highest_since_entry > 0
                and (pos.highest_since_entry - c) >= 2.0 * atr):
            return True, f"bear:trail_2xATR(from{pos.highest_since_entry:.1f})"

    # baseline 條件對 bull / neutral 都套用
    if _trend_break_s8(row, 3.0 if tag != "bear" else 2.0):
        return True, f"S8_{tag}"
    if _ma20_break_red3(row):
        return True, "S9_red3+MA20"
    return False, ""


# ─────────────────────────────────────────────────────────────────
# S5: Hybrid — 雙條件主動觸發（個股級，不依賴 TAIEX）
#   觸發 = (highest_since_entry - close) >= X×ATR  AND  close < EMA21
#   兩條件同時成立才賣：
#     - 多頭時 EMA21 不破 → 不觸發 → 不被洗
#     - 急殺時 ATR 跌幅與 EMA21 break 幾乎同時成立 → 立刻出
#   保留 baseline 的 S9 (連3黑+MA20) 作為強化條件
# ─────────────────────────────────────────────────────────────────
def _hybrid(atr_mult: float):
    """工廠：產生指定 ATR 倍數的 S5 變體"""
    def _impl(row, pos, taiex_row) -> Tuple[bool, str]:
        c = row.get('close')
        ema21 = row.get('ema21')
        atr = row.get('atr14')
        if (pd.notna(c) and pd.notna(ema21) and pd.notna(atr)
                and atr > 0 and pos.highest_since_entry > 0):
            drop = pos.highest_since_entry - c
            if drop >= atr_mult * atr and c < ema21:
                return True, (f"hybrid_{atr_mult}xATR+EMA21"
                              f"(from{pos.highest_since_entry:.1f},drop{drop/atr:.1f}xATR)")
        # 保留 S9
        if _ma20_break_red3(row):
            return True, "S9_red3+MA20"
        return False, ""
    return _impl


hybrid_2_0 = _hybrid(2.0)
hybrid_2_5 = _hybrid(2.5)
hybrid_3_0 = _hybrid(3.0)


# ─────────────────────────────────────────────────────────────────
# S6: Either — 「OR 條件」：trailing X×ATR  OR  close < EMA21
#   任一成立就賣：急殺時誰先到都觸發 → 比 S5 (AND) 快
#   多頭時 EMA21 break 會觸發 → 可能像 S2 ma_break 一樣偏敏感
# ─────────────────────────────────────────────────────────────────
def _either(atr_mult: float):
    def _impl(row, pos, taiex_row) -> Tuple[bool, str]:
        c = row.get('close')
        ema21 = row.get('ema21')
        atr = row.get('atr14')
        if (pd.notna(c) and pd.notna(ema21) and pd.notna(atr) and atr > 0
                and pos.highest_since_entry > 0):
            drop = pos.highest_since_entry - c
            if drop >= atr_mult * atr:
                return True, (f"either_{atr_mult}xATR"
                              f"(from{pos.highest_since_entry:.1f},drop{drop/atr:.1f}xATR)")
            if c < ema21:
                return True, f"either_close<EMA21"
        if _ma20_break_red3(row):
            return True, "S9_red3+MA20"
        return False, ""
    return _impl


either_2_0 = _either(2.0)
either_2_5 = _either(2.5)
either_3_0 = _either(3.0)


# ─────────────────────────────────────────────────────────────────
# S7: S6 + 個股 RegimeLayer 收緊
#   個股 RegimeLayer 顯示「空頭 / 高檔轉折 / 盤整」 → ATR 倍數收緊到 1.5×
#   其它（強勢多頭/多頭/底部轉強） → 用 2.0×（S6 預設）
#   個股 regime 比 TAIEX MA200 反應更快（3-5 日 vs 2-3 週）
# ─────────────────────────────────────────────────────────────────
TIGHT_REGIMES = {"空頭", "高檔轉折", "盤整"}


def s7_adaptive_either(row, pos, taiex_row) -> Tuple[bool, str]:
    c = row.get('close')
    ema21 = row.get('ema21')
    atr = row.get('atr14')
    regime = str(row.get('regime', '') or '')
    mult = 1.5 if regime in TIGHT_REGIMES else 2.0

    if (pd.notna(c) and pd.notna(ema21) and pd.notna(atr) and atr > 0
            and pos.highest_since_entry > 0):
        drop = pos.highest_since_entry - c
        if drop >= mult * atr:
            return True, (f"S7_{mult}xATR[{regime or 'no_reg'}]"
                          f"(from{pos.highest_since_entry:.1f},drop{drop/atr:.1f}xATR)")
        if c < ema21:
            return True, f"S7_close<EMA21[{regime or 'no_reg'}]"
    if _ma20_break_red3(row):
        return True, "S9_red3+MA20"
    return False, ""


# ─────────────────────────────────────────────────────────────────
# S8: 非對稱 adaptive — 空頭區收緊、多頭區放寬
#   空頭/盤整/高檔轉折 → 1.5×ATR（保護）
#   強勢多頭/多頭/底部轉強 → 2.5×ATR（不被洗）
#   無 regime → 2.0×（折衷）
# ─────────────────────────────────────────────────────────────────
LOOSE_REGIMES = {"強勢多頭", "多頭", "底部轉強"}


def s8_asymmetric(row, pos, taiex_row) -> Tuple[bool, str]:
    c = row.get('close')
    ema21 = row.get('ema21')
    atr = row.get('atr14')
    regime = str(row.get('regime', '') or '')
    if regime in TIGHT_REGIMES:
        mult = 1.5
    elif regime in LOOSE_REGIMES:
        mult = 2.5
    else:
        mult = 2.0

    if (pd.notna(c) and pd.notna(ema21) and pd.notna(atr) and atr > 0
            and pos.highest_since_entry > 0):
        drop = pos.highest_since_entry - c
        if drop >= mult * atr:
            return True, f"S8_{mult}xATR[{regime or 'no_reg'}]"
        if c < ema21:
            return True, f"S8_close<EMA21[{regime or 'no_reg'}]"
    if _ma20_break_red3(row):
        return True, "S9_red3+MA20"
    return False, ""


STRATEGIES = {
    "S0_baseline": baseline,
    "S1_trailing_atr": trailing_atr,
    "S2_ma_break": ma_break,
    "S3_adaptive": adaptive_trend_break,
    "S4_regime_combo": regime_combo,
    "S5_hybrid_2.0x": hybrid_2_0,
    "S5_hybrid_2.5x": hybrid_2_5,
    "S5_hybrid_3.0x": hybrid_3_0,
    "S6_either_2.0x": either_2_0,
    "S6_either_2.5x": either_2_5,
    "S6_either_3.0x": either_3_0,
    "S7_adaptive_either": s7_adaptive_either,
    "S8_asymmetric": s8_asymmetric,
}
