"""sector_auto_trader 除權息參考價守衛（_ex_dividend_ref）單元測試（不打網路）。"""

import os
import sys

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import sector_auto_trader as sat


class _Resp:
    def __init__(self, records):
        self._records = records

    def json(self):
        return {"msg": "success", "data": self._records}


def _patch(monkeypatch, records):
    sat._div_ref_cache.clear()
    monkeypatch.setattr(sat.requests, "get", lambda *a, **k: _Resp(records))


def test_ex_dividend_in_window_returns_ref(monkeypatch):
    """除息生效落在 (前一交易日, 最新交易日] → 回除息參考價（3034 情境：假日順延）。"""
    _patch(monkeypatch, [{"date": "2026-07-10", "before_price": 542.0, "after_price": 519.0}])
    assert sat._ex_dividend_ref("3034.TW", "2026-07-09", "2026-07-13") == 519.0


def test_ex_dividend_boundary_cur_date_inclusive(monkeypatch):
    _patch(monkeypatch, [{"date": "2026-07-13", "after_price": 100.0}])
    assert sat._ex_dividend_ref("1234.TW", "2026-07-09", "2026-07-13") == 100.0


def test_ex_dividend_outside_window_none(monkeypatch):
    # 除息在前一交易日之前（舊）→ 不適用
    _patch(monkeypatch, [{"date": "2026-07-08", "after_price": 100.0}])
    assert sat._ex_dividend_ref("1234.TW", "2026-07-09", "2026-07-13") is None
    # 除息在最新交易日之後（未來）→ 不適用
    _patch(monkeypatch, [{"date": "2026-07-20", "after_price": 100.0}])
    assert sat._ex_dividend_ref("1234.TW", "2026-07-09", "2026-07-13") is None


def test_ex_dividend_non_numeric_symbol_none(monkeypatch):
    _patch(monkeypatch, [{"date": "2026-07-10", "after_price": 519.0}])
    assert sat._ex_dividend_ref("^TWII", "2026-07-09", "2026-07-13") is None


def test_ex_dividend_bad_ref_price_none(monkeypatch):
    _patch(monkeypatch, [{"date": "2026-07-10", "after_price": 0}])       # 0/缺 → 不用
    assert sat._ex_dividend_ref("1234.TW", "2026-07-09", "2026-07-13") is None


def test_ex_dividend_fetch_failure_none(monkeypatch):
    sat._div_ref_cache.clear()
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(sat.requests, "get", boom)
    assert sat._ex_dividend_ref("1234.TW", "2026-07-09", "2026-07-13") is None
    assert "1234" not in sat._div_ref_cache          # 失敗不快取（下次重試、不 silent stale）
    # 之後成功 → 恢復正常回參考價
    monkeypatch.setattr(sat.requests, "get",
                        lambda *a, **k: _Resp([{"date": "2026-07-10", "after_price": 519.0}]))
    assert sat._ex_dividend_ref("1234.TW", "2026-07-09", "2026-07-13") == 519.0
