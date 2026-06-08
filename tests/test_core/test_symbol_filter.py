"""Per-symbol BUY filter 單元測試。

對應 backend/sector_auto_trader.py::should_pass_symbol_filter。
覆蓋：
- A_volume 量能 filter 純函式邊界
- SYMBOL_BUY_FILTER 字典守門（2382 / 2881 必須在；2317/2882/2891/2383 必須不在）
- M1 fix 行為鎖定：filter 擋下 standard_buy 時，pullback/rebound 退路是否保留
"""

import pandas as pd
import pytest

from sector_auto_trader import (
    SYMBOL_BUY_FILTER,
    _filter_a_volume_check,
    should_pass_symbol_filter,
)


def _make_df(volumes: list[float]) -> pd.DataFrame:
    n = len(volumes)
    return pd.DataFrame({
        "open":   [100.0] * n,
        "high":   [101.0] * n,
        "low":    [99.0] * n,
        "close":  [100.0] * n,
        "volume": volumes,
    })


# ── A_volume 純函式 ─────────────────────────────────────────

def test_a_volume_pass_when_today_above_1_5x():
    df = _make_df([1000.0] * 20 + [1600.0])
    ok, ratio = _filter_a_volume_check(df)
    assert ok is True
    assert ratio == pytest.approx(1.6)


def test_a_volume_block_when_today_under_1_5x():
    df = _make_df([1000.0] * 20 + [1400.0])
    ok, ratio = _filter_a_volume_check(df)
    assert ok is False
    assert ratio == pytest.approx(1.4)


def test_a_volume_insufficient_history_does_not_block():
    df = _make_df([1000.0] * 10)
    ok, _ = _filter_a_volume_check(df)
    assert ok is True


def test_a_volume_avg20_zero_does_not_block():
    df = _make_df([0.0] * 20 + [500.0])
    ok, _ = _filter_a_volume_check(df)
    assert ok is True


# ── 字典守門 ─────────────────────────────────────────

def test_should_pass_unlisted_symbol_always_true():
    df = _make_df([1000.0] * 20 + [100.0])  # 量縮但 symbol 未列入
    ok, detail = should_pass_symbol_filter("9999", df)
    assert ok is True
    assert detail == ""


def test_2382_in_filter_dict_with_a_volume():
    """2382 廣達：回測 +67.3pp + MDD -6pp，必須在字典裡。"""
    assert SYMBOL_BUY_FILTER.get("2382") == "A_volume"


def test_2881_in_filter_dict_with_a_volume():
    """2881 富邦金：回測 +34pp + MDD -14pp，必須在字典裡。"""
    assert SYMBOL_BUY_FILTER.get("2881") == "A_volume"


def test_2382_volume_filter_blocks_low_volume():
    df = _make_df([1000.0] * 20 + [800.0])  # ratio=0.8
    ok, detail = should_pass_symbol_filter("2382", df)
    assert ok is False
    assert "A_volume擋" in detail


def test_2881_volume_filter_passes_high_volume():
    df = _make_df([1000.0] * 20 + [2000.0])  # ratio=2.0
    ok, detail = should_pass_symbol_filter("2881", df)
    assert ok is True
    assert "A_volume過" in detail


def test_all_listed_symbols_recognized():
    """字典裡每個 symbol 都應該被識別並能執行 filter（非 silently 全通過）。"""
    df = _make_df([1000.0] * 20 + [800.0])  # 量縮
    for sym in SYMBOL_BUY_FILTER:
        ok, detail = should_pass_symbol_filter(sym, df)
        assert ok is False, f"{sym} 應該被量縮擋下但通過了"
        assert detail, f"{sym} 應該回傳描述"


# ── 字典反向守門：曾考慮但證據不足的 symbol 必須不在 ──

@pytest.mark.parametrize("sym,reason", [
    ("2317", "MDD 改善型；return 持平不符合 ≥30pp 標準"),
    ("2882", "return +0.5pp、MDD -5pp，證據不足"),
    ("2891", "A_volume 報酬比 baseline 差 -10pp"),
    ("2383", "baseline 是最佳，任何 filter 都拖累"),
])
def test_excluded_symbols_not_in_filter_dict(sym, reason):
    assert sym not in SYMBOL_BUY_FILTER, f"{sym} 不該列入：{reason}"


# ── M1 行為鎖定：filter 擋下 standard_buy 時，pullback 退路不能被吞 ──
#
# 完整 process_sector 整合測試需 mock 10+ 依賴（fetch_signal_data, compute_signal,
# 各層、broker、market_hours...），暫列 followup。這裡用最小邏輯複刻驗證 M1 修法。
# 原 commit 21d3633 bug：filter 擋下 standard_buy 後直接 continue，
# 把 pullback_buy=True 的 70% 倉位退路也吞掉。

def _simulate_entry_decision(standard_buy, pullback_buy, rebound_buy, filter_pass):
    """複刻 sector_auto_trader.py process_sector 的 BUY-side decision flow。

    回傳：(action, ratio_multiplier, path)
      action: "BUY" or "SKIP"
      ratio_multiplier: 1.0 (standard) or 0.7 (pullback/rebound)
      path: "standard", "pullback", "rebound", or None
    """
    if not (standard_buy or pullback_buy or rebound_buy):
        return "SKIP", 0, None
    # 對應 process_sector 新邏輯（修 M1 後）：
    if standard_buy:
        if not filter_pass:
            if pullback_buy or rebound_buy:
                standard_buy = False  # 降級退路
            else:
                return "SKIP", 0, None
    if pullback_buy and not standard_buy:
        return "BUY", 0.7, "pullback"
    elif rebound_buy and not standard_buy:
        return "BUY", 0.7, "rebound"
    else:
        return "BUY", 1.0, "standard"


@pytest.mark.parametrize("standard,pullback,rebound,filter_pass,exp_action,exp_ratio,exp_path", [
    # 基本：filter 過、走 standard
    (True,  False, False, True,  "BUY",  1.0, "standard"),
    # filter 擋、無退路 → SKIP
    (True,  False, False, False, "SKIP", 0,   None),
    # M1 核心：filter 擋 standard，但 pullback 退路在 → 降級走 pullback 70%
    (True,  True,  False, False, "BUY",  0.7, "pullback"),
    # M1 核心：filter 擋 standard，rebound 退路在 → 降級走 rebound 70%
    (True,  False, True,  False, "BUY",  0.7, "rebound"),
    # filter 過 + 同時有 pullback → 走 standard full size（原行為，不變）
    (True,  True,  False, True,  "BUY",  1.0, "standard"),
    # 純 pullback (standard 不成立) → filter 不套用
    (False, True,  False, False, "BUY",  0.7, "pullback"),
    # 純 rebound → filter 不套用
    (False, False, True,  False, "BUY",  0.7, "rebound"),
    # 沒任何 buy 訊號 → SKIP
    (False, False, False, True,  "SKIP", 0,   None),
])
def test_entry_decision_matrix(standard, pullback, rebound, filter_pass,
                                exp_action, exp_ratio, exp_path):
    action, ratio, path = _simulate_entry_decision(standard, pullback, rebound, filter_pass)
    assert action == exp_action
    assert ratio == exp_ratio
    assert path == exp_path
