"""Per-symbol BUY filter 單元測試。

對應 backend/sector_auto_trader.py::should_pass_symbol_filter，
確保證據明確的調整（如 2317/2382/金融三檔套 A_volume）能正確擋下量縮假突破。
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


def test_a_volume_pass_when_today_above_1_5x():
    # avg20=1000、today=1600 → ratio=1.6 通過
    df = _make_df([1000.0] * 20 + [1600.0])
    ok, ratio = _filter_a_volume_check(df)
    assert ok is True
    assert ratio == pytest.approx(1.6)


def test_a_volume_block_when_today_under_1_5x():
    # avg20=1000、today=1400 → ratio=1.4 不通過
    df = _make_df([1000.0] * 20 + [1400.0])
    ok, ratio = _filter_a_volume_check(df)
    assert ok is False
    assert ratio == pytest.approx(1.4)


def test_a_volume_insufficient_history_does_not_block():
    # 只有 10 根 K → 通過，不阻擋新上市股
    df = _make_df([1000.0] * 10)
    ok, _ = _filter_a_volume_check(df)
    assert ok is True


def test_a_volume_avg20_zero_does_not_block():
    df = _make_df([0.0] * 20 + [500.0])
    ok, _ = _filter_a_volume_check(df)
    assert ok is True


def test_should_pass_unlisted_symbol_always_true():
    """未列入 SYMBOL_BUY_FILTER 的 symbol 一律通過。"""
    df = _make_df([1000.0] * 20 + [100.0])  # 量縮但 symbol 未列入
    ok, detail = should_pass_symbol_filter("9999", df)
    assert ok is True
    assert detail == ""


def test_should_pass_2317_volume_filter_blocks_low_volume():
    df = _make_df([1000.0] * 20 + [800.0])  # 量縮：ratio=0.8
    ok, detail = should_pass_symbol_filter("2317", df)
    assert ok is False
    assert "A_volume擋" in detail


def test_should_pass_2317_volume_filter_passes_high_volume():
    df = _make_df([1000.0] * 20 + [2000.0])  # 量增：ratio=2.0
    ok, detail = should_pass_symbol_filter("2317", df)
    assert ok is True
    assert "A_volume過" in detail


def test_all_listed_symbols_recognized():
    """所有列入字典的 symbol 都應該被識別（不會 silently 全通過）。"""
    df = _make_df([1000.0] * 20 + [800.0])  # 量縮
    for sym in SYMBOL_BUY_FILTER:
        ok, detail = should_pass_symbol_filter(sym, df)
        assert ok is False, f"{sym} 應該被量縮擋下但通過了"
        assert detail, f"{sym} 應該回傳描述"


def test_2383_not_in_filter_dict():
    """2383 台光電：baseline 是最佳（任何 filter 都拖累），明示不能在字典裡。"""
    assert "2383" not in SYMBOL_BUY_FILTER
