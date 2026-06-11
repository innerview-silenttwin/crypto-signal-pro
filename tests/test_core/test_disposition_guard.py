"""處置股 guard 單元測試。

對應 backend/brokers/disposition_guard.py。
覆蓋：
- punish() 物件 parallel list 解析（多檔、加重處置 unit_limit=None）
- 每日 cache + 失敗 fallback
- ensure_sellable 分支：sim / prod 服務時段內外 / 圈存門檻判斷 / reserve_stock 成敗
- snapshot 完整 schema、symbol 正規化、telegram 去重

來源：2026-06-11 永豐客服 dump 確認的物件結構。
"""

import datetime
from unittest.mock import MagicMock

import pytest

from brokers.disposition_guard import DispositionGuard, _INFO_FIELDS


# ── helpers ─────────────────────────────────────────

def _make_punish(stocks: list[dict]):
    """模擬 api.punish() 回傳：物件含 9 個 parallel list。

    stocks: [{"code": "6770", "start_date": date(...), "end_date": date(...),
              "interval": "5分鐘", "unit_limit": 10.0, "total_limit": 30.0,
              "description": "..."}, ...]
    """
    obj = MagicMock()
    obj.code = [s["code"] for s in stocks]
    for field in _INFO_FIELDS:
        setattr(obj, field, [s.get(field) for s in stocks])
    return obj


def _make_api(stocks=None, *, raise_exc=False):
    api = MagicMock()
    if raise_exc:
        api.punish.side_effect = RuntimeError("network down")
    else:
        api.punish.return_value = _make_punish(stocks or [])
    return api


def _stock_6770_normal():
    """5 分鐘處置（一般等級），unit=10、total=30。"""
    return {
        "code": "6770",
        "start_date": datetime.date(2026, 6, 2),
        "end_date": datetime.date(2026, 6, 15),
        "announced_date": datetime.date(2026, 6, 1),
        "interval": "5分鐘",
        "unit_limit": 10.0,
        "total_limit": 30.0,
        "description": "處置原因：xxx",
    }


def _stock_strict():
    """20 分鐘加重處置，unit/total = None。"""
    return {
        "code": "1234",
        "start_date": datetime.date(2026, 6, 1),
        "end_date": datetime.date(2026, 6, 20),
        "announced_date": datetime.date(2026, 5, 31),
        "interval": "20分鐘",
        "unit_limit": None,
        "total_limit": None,
        "description": "近期再犯",
    }


class FakeReserveResponse:
    def __init__(self, status, info=""):
        self.response = MagicMock(status=status, info=info)


# ── punish() 物件解析 ─────────────────────────────────────────

def test_punish_parses_parallel_lists():
    g = DispositionGuard(simulation=True)
    api = _make_api([_stock_6770_normal(), _stock_strict()])
    codes = g.get_disposition_set(api)
    assert codes == {"6770", "1234"}

    info_6770 = g.get_disposition_info(api, "6770.TW")
    assert info_6770["interval"] == "5分鐘"
    assert info_6770["unit_limit"] == 10.0
    assert info_6770["end_date"] == datetime.date(2026, 6, 15)

    info_strict = g.get_disposition_info(api, "1234")
    assert info_strict["interval"] == "20分鐘"
    assert info_strict["unit_limit"] is None


def test_punish_empty_list_valid():
    g = DispositionGuard(simulation=True)
    api = _make_api([])
    assert g.get_disposition_set(api) == set()
    assert g.snapshot()["ok"] is True


def test_punish_same_day_cached():
    g = DispositionGuard(simulation=True)
    api = _make_api([_stock_6770_normal()])
    g.get_disposition_set(api)
    g.get_disposition_set(api)
    g.get_disposition_set(api)
    api.punish.assert_called_once()


def test_punish_exception_sets_ok_false():
    g = DispositionGuard(simulation=True)
    api = _make_api(raise_exc=True)
    g.get_disposition_set(api)
    assert g.snapshot()["ok"] is False


def test_punish_failure_after_success_keeps_old_data():
    """成功載入後再次 refresh 失敗時，保留舊資料 + ok=False。

    模擬跨日：強制 _date 改為昨天，下次 _ensure_today 會嘗試 refresh，refresh 失敗。
    """
    g = DispositionGuard(simulation=False)
    api = _make_api([_stock_6770_normal()])
    g.get_disposition_set(api)
    assert g.snapshot()["ok"] is True
    # 模擬「跨日 → refresh 失敗」
    g._date = datetime.date(2000, 1, 1)
    api.punish.side_effect = RuntimeError("transient")
    g.get_disposition_set(api)
    assert g.snapshot()["ok"] is False
    assert "6770" in g.snapshot()["stocks"]  # 舊資料保留


# ── symbol normalization ─────────────────────────────────────────

def test_normalize_strips_tw_suffix():
    assert DispositionGuard._normalize("6770.TW") == "6770"
    assert DispositionGuard._normalize("6223.TWO") == "6223"
    assert DispositionGuard._normalize("6770") == "6770"


def test_is_disposed_handles_tw_suffix():
    g = DispositionGuard(simulation=True)
    api = _make_api([_stock_6770_normal()])
    assert g.is_disposed(api, "6770.TW") is True
    assert g.is_disposed(api, "6770") is True
    assert g.is_disposed(api, "2330.TW") is False


# ── ensure_sellable: 非處置股 ─────────────────────────────────────────

def test_ensure_sellable_non_disposed_passes():
    g = DispositionGuard(simulation=True)
    api = _make_api([_stock_6770_normal()])
    ok, reason = g.ensure_sellable(api, "2330.TW", 1000)
    assert ok is True


# ── ensure_sellable: sim ─────────────────────────────────────────

def test_ensure_sellable_sim_blocks():
    g = DispositionGuard(simulation=True)
    api = _make_api([_stock_6770_normal()])
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000)
    assert ok is False
    assert "disposition_sim_noop" in reason


# ── ensure_sellable: prod 圈存門檻 ─────────────────────────────────────────

def _prod_setup(stocks):
    g = DispositionGuard(simulation=False)
    api = _make_api(stocks)
    g.get_disposition_set(api)
    return g, api


def test_prod_small_order_below_unit_limit_passes_without_reserve():
    """5 分鐘處置 + qty(張)=1 < unit_limit=10 → 直接 SELL 不 reserve。

    這是 6770 真實情境：我們持倉 1 張，本來就不該強迫走 reserve_stock。
    """
    g, api = _prod_setup([_stock_6770_normal()])
    now = datetime.datetime(2026, 6, 11, 10, 0)
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000, now=now)
    assert ok is True
    assert reason == ""
    api.reserve_stock.assert_not_called()  # 沒呼叫 reserve_stock


def test_prod_order_above_unit_limit_triggers_reserve():
    """qty=15000 股 = 15 張 ≥ unit_limit=10 → 走 reserve_stock。"""
    g, api = _prod_setup([_stock_6770_normal()])
    api.Contracts.Stocks.__getitem__.return_value = MagicMock(name="contract")
    api.reserve_stock.return_value = FakeReserveResponse(status=True)
    now = datetime.datetime(2026, 6, 11, 10, 0)
    ok, reason = g.ensure_sellable(api, "6770.TW", 15000, now=now)
    assert ok is True
    api.reserve_stock.assert_called_once()


def test_prod_strict_disposition_always_needs_reserve():
    """20 分鐘加重處置 + unit_limit=None → 每筆都要 reserve（即使小單）。"""
    g, api = _prod_setup([_stock_strict()])
    api.Contracts.Stocks.__getitem__.return_value = MagicMock(name="contract")
    api.reserve_stock.return_value = FakeReserveResponse(status=True)
    now = datetime.datetime(2026, 6, 11, 10, 0)
    ok, reason = g.ensure_sellable(api, "1234", 100, now=now)  # 0.1 張，很小
    assert ok is True
    api.reserve_stock.assert_called_once()


def test_prod_reserve_status_false_blocks():
    g, api = _prod_setup([_stock_strict()])
    api.Contracts.Stocks.__getitem__.return_value = MagicMock(name="contract")
    api.reserve_stock.return_value = FakeReserveResponse(status=False, info="quota exceeded")
    now = datetime.datetime(2026, 6, 11, 10, 0)
    ok, reason = g.ensure_sellable(api, "1234", 1000, now=now)
    assert ok is False
    assert "reserve_stock_status_false" in reason


def test_prod_reserve_exception_blocks():
    g, api = _prod_setup([_stock_strict()])
    api.Contracts.Stocks.__getitem__.return_value = MagicMock(name="contract")
    api.reserve_stock.side_effect = TimeoutError("api down")
    now = datetime.datetime(2026, 6, 11, 10, 0)
    ok, reason = g.ensure_sellable(api, "1234", 1000, now=now)
    assert ok is False
    assert "reserve_stock_exception" in reason


def test_prod_out_of_hours_blocks_even_small_order():
    """非服務時段一律擋，即使小單也是。"""
    g, api = _prod_setup([_stock_6770_normal()])
    now = datetime.datetime(2026, 6, 11, 7, 0)
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000, now=now)
    assert ok is False
    assert "disposition_out_of_hours" in reason


def test_prod_list_not_loaded_blocks():
    g = DispositionGuard(simulation=False)
    api = _make_api(raise_exc=True)
    g.get_disposition_set(api)
    # 模擬「曾載入但目前 refresh 失敗」狀態
    g._info = {"6770": _stock_6770_normal()}
    g._ok = False
    now = datetime.datetime(2026, 6, 11, 10, 0)
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000, now=now)
    assert ok is False
    assert "disposition_list_not_loaded" in reason


# ── 服務時段邊界 ─────────────────────────────────────────

@pytest.mark.parametrize("hh,mm,exp", [
    (7, 59, False),
    (8, 0, True),
    (8, 30, True),
    (14, 30, True),
    (14, 31, False),
    (23, 0, False),
])
def test_in_reserve_hours_boundary(hh, mm, exp):
    now = datetime.datetime(2026, 6, 11, hh, mm)
    assert DispositionGuard._in_reserve_hours(now) is exp


# ── _need_reserve 純函式 ─────────────────────────────────────────

@pytest.mark.parametrize("unit,total,qty_lots,exp", [
    # 加重處置：unit=None → 一律 True
    (None, None, 0.1, True),
    (None, None, 100, True),
    # 一般處置 unit=10：小於 → False，大於等於 → True
    (10.0, 30.0, 1.0, False),
    (10.0, 30.0, 9.999, False),
    (10.0, 30.0, 10.0, True),
    (10.0, 30.0, 50.0, True),
])
def test_need_reserve_matrix(unit, total, qty_lots, exp):
    entry = {"unit_limit": unit, "total_limit": total}
    assert DispositionGuard._need_reserve(entry, qty_lots) is exp


# ── snapshot 完整 schema ─────────────────────────────────────────

def test_snapshot_includes_full_info():
    g = DispositionGuard(simulation=True)
    api = _make_api([_stock_6770_normal()])
    g.get_disposition_set(api)
    snap = g.snapshot()
    assert snap["ok"] is True
    assert snap["count"] == 1
    assert "stocks" in snap
    s = snap["stocks"]["6770"]
    assert s["start_date"] == "2026-06-02"
    assert s["end_date"] == "2026-06-15"
    assert s["interval"] == "5分鐘"
    assert s["unit_limit"] == 10.0


def test_snapshot_empty_does_not_call_punish():
    g = DispositionGuard(simulation=True)
    api = _make_api([_stock_6770_normal()])
    snap = g.snapshot()  # 尚未呼叫 get_disposition_set
    assert snap == {"date": None, "ok": False, "count": 0, "stocks": {}}
    api.punish.assert_not_called()


# ── telegram 去重 ─────────────────────────────────────────

def test_should_send_daily_telegram_once_per_day():
    g = DispositionGuard(simulation=True)
    assert g.should_send_daily_telegram() is True
    assert g.should_send_daily_telegram() is False
    assert g.should_send_daily_telegram() is False


# ── _iso 序列化 helper ─────────────────────────────────────────

from brokers.disposition_guard import _iso  # noqa: E402


@pytest.mark.parametrize("inp,exp", [
    (None, None),
    (datetime.date(2026, 6, 15), "2026-06-15"),
    (datetime.datetime(2026, 6, 11, 14, 30), "2026-06-11T14:30:00"),
    ("already string", "already string"),
    (42, "42"),
])
def test_iso_helper(inp, exp):
    assert _iso(inp) == exp


# ── 不持鎖跨網路呼叫（M1 race safety verification）─────────────

def test_punish_call_does_not_hold_main_lock():
    """確認 api.punish() 被呼叫時 self._lock 沒被持有，避免其它 thread 卡死。

    若 punish() 期間 self._lock 仍被持有，broker hang 時 UI / 其它 SELL 路徑都會卡。
    """
    g = DispositionGuard(simulation=True)
    lock_held_during_punish = []

    def punish_side_effect(*a, **kw):
        # 嘗試非阻塞拿 _lock；拿到 = 沒被佔 = OK
        got = g._lock.acquire(blocking=False)
        lock_held_during_punish.append(not got)
        if got:
            g._lock.release()
        return _make_punish([_stock_6770_normal()])

    api = MagicMock()
    api.punish.side_effect = punish_side_effect
    g.get_disposition_set(api)
    assert lock_held_during_punish == [False], "punish() 期間不該持有 _lock"
