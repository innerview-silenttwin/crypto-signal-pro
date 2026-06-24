"""etf_compare 純計算函式單元測試（不打網路，用合成 Series）。

驗證區間報酬 / 最大回撤 / 基準日對齊 / 窗口解析 / 比較池合併。
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import etf_compare as ec


def _series(pairs):
    idx = pd.to_datetime([d for d, _ in pairs])
    return pd.Series([v for _, v in pairs], index=idx)


def test_norm_ticker():
    assert ec._norm_etf_ticker("0056") == "0056.TW"
    assert ec._norm_etf_ticker("00981a.TW") == "00981A.TW"
    assert ec._norm_etf_ticker(" 00982A.TWO ") == "00982A.TW"


def test_period_return_up_and_down():
    s = _series([("2026-06-01", 100), ("2026-06-02", 110), ("2026-06-03", 99)])
    # 漲：100 → 110 = +10%
    assert ec._period_return(s, "2026-06-01", "2026-06-02") == pytest.approx(10.0)
    # 跌：100 → 99 = -1%
    assert ec._period_return(s, "2026-06-01", "2026-06-03") == pytest.approx(-1.0)


def test_close_asof_uses_on_or_before():
    s = _series([("2026-06-01", 100), ("2026-06-03", 105)])
    # 06-02 無資料 → 取 06-01 的 100
    assert ec._close_asof(s, "2026-06-02") == 100.0
    # 06-03 當日
    assert ec._close_asof(s, "2026-06-03") == 105.0
    # 早於所有資料 → NaN
    assert np.isnan(ec._close_asof(s, "2026-05-01"))


def test_max_drawdown():
    # 100 → 120(peak) → 90(trough) → 95：最大回撤 = (90-120)/120 = -25%
    s = _series([("2026-06-01", 100), ("2026-06-02", 120),
                 ("2026-06-03", 90), ("2026-06-04", 95)])
    assert ec._max_drawdown(s, "2026-06-01", "2026-06-04") == pytest.approx(-25.0)


def test_max_drawdown_no_drop():
    s = _series([("2026-06-01", 100), ("2026-06-02", 105), ("2026-06-03", 110)])
    assert ec._max_drawdown(s, "2026-06-01", "2026-06-03") == pytest.approx(0.0)


def test_max_drawdown_insufficient_data():
    s = _series([("2026-06-01", 100)])
    assert np.isnan(ec._max_drawdown(s, "2026-06-01", "2026-06-01"))


def test_period_return_nan_on_missing():
    s = _series([("2026-06-01", 100)])
    assert np.isnan(ec._period_return(s, "2026-05-01", "2026-05-02"))


def test_base_close_full_coverage():
    s = _series([("2026-06-01", 100), ("2026-06-05", 110)])
    price, date, partial = ec._base_close(s, "2026-06-01", "2026-06-05")
    assert price == 100.0 and partial is False


def test_base_close_partial_when_inception_inside_window():
    # ETF 在 06-15 才掛牌，但窗口從 06-01 開始 → 用窗口內第一筆當基準、partial=True
    s = _series([("2026-06-15", 50), ("2026-06-20", 55)])
    price, date, partial = ec._base_close(s, "2026-06-01", "2026-06-24")
    assert price == 50.0
    assert date == "2026-06-15"
    assert partial is True


def test_period_return_uses_partial_base_not_nan():
    # 掛牌在窗口內：報酬 = 自掛牌起 (55/50-1)=+10%，而非 NaN
    s = _series([("2026-06-15", 50), ("2026-06-20", 55)])
    r = ec._period_return(s, "2026-06-01", "2026-06-24")
    assert r == pytest.approx(10.0)


def test_resolve_window_defaults():
    start, end = ec.resolve_window(None, "2026-06-24")
    assert end == "2026-06-24"
    # 預設 start = end - 30 天
    assert start == "2026-05-25"


def test_resolve_window_explicit():
    assert ec.resolve_window("2026-06-01", "2026-06-10") == ("2026-06-01", "2026-06-10")


def test_compare_output_json_serializable(monkeypatch):
    """compare() 回傳必須是原生型別（np.bool_/np.float 會讓 FastAPI 序列化爆掉）。"""
    import json

    monkeypatch.setattr(ec, "build_compare_pool", lambda: [
        {"code": "00981A", "name": "A", "source": "alpha"},
        {"code": "ZZZZ", "name": "Z", "source": "custom"},  # 無資料 → ok=False 分支
    ])
    s = _series([("2026-06-01", 100), ("2026-06-02", 90)])
    monkeypatch.setattr(ec, "_fetch_closes", lambda tickers, start, end: {
        "00981A.TW": s, "^TWII": _series([("2026-06-01", 100), ("2026-06-02", 95)]),
    })
    out = ec.compare(start="2026-06-01", end="2026-06-02")
    # 不能丟出 TypeError（json.dumps 對 numpy 型別會爆）
    json.dumps(out)
    row = next(e for e in out["etfs"] if e["code"] == "00981A")
    assert isinstance(row["ok"], bool)
    assert row["beat_ret"] in (True, False)
    assert type(row["beat_ret"]) is bool          # 不能是 numpy.bool_
    # 00981A 跌 10% vs 大盤跌 5% → 跑輸大盤
    assert row["beat_ret"] is False


def test_build_pool_merges_and_dedups(monkeypatch):
    # alpha 池含 00981A；watchlist 也加 00981A（應去重）+ 一個新的 0056
    monkeypatch.setattr(ec, "_BENCHMARK_NAMES", ec._BENCHMARK_NAMES)  # noop, 確保模組可 patch

    import layers.active_etf as ae
    monkeypatch.setattr(ae, "BEAT_ETFS", [{"code": "00981A", "name": "統一台股增長"}])
    import settings_manager as sm
    monkeypatch.setattr(sm, "get_watch_etfs", lambda: [
        {"code": "00981A", "name": "重複"}, {"code": "0056", "name": "高股息"}])

    pool = ec.build_compare_pool()
    codes = [p["code"] for p in pool]
    assert codes.count("00981A") == 1                 # 去重
    assert "0056" in codes
    src = {p["code"]: p["source"] for p in pool}
    assert src["00981A"] == "alpha"                   # alpha 優先
    assert src["0056"] == "custom"
