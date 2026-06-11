"""處置股 guard 單元測試。

對應 backend/brokers/disposition_guard.py，覆蓋：
- punish() 清單快取（跨日 refresh、失敗 fallback）
- ensure_sellable 分支：非處置股 / sim / prod 服務時段內外 / reserve_stock 成功失敗
- symbol 格式正規化（.TW 後綴）
- snapshot / should_send_daily_telegram

來源：2026-06-11 永豐客服範例骨架 + 整合 review。
"""

import datetime
from unittest.mock import MagicMock

import pytest

from brokers.disposition_guard import DispositionGuard, RESERVE_START, RESERVE_END


# ── helpers ─────────────────────────────────────────

class FakePunishResult:
    def __init__(self, codes):
        self.code = codes


class FakeReserveResponse:
    def __init__(self, status, info=""):
        self.response = MagicMock(status=status, info=info)


def _api_with_punish(codes, *, raise_exc=False):
    api = MagicMock()
    if raise_exc:
        api.punish.side_effect = RuntimeError("network down")
    else:
        api.punish.return_value = FakePunishResult(codes)
    return api


# ── get_disposition_set / refresh ─────────────────────────────────────────

def test_punish_first_call_loads_set():
    g = DispositionGuard(simulation=True)
    api = _api_with_punish(["6770", "2330"])
    codes = g.get_disposition_set(api)
    assert codes == {"6770", "2330"}
    assert g.snapshot()["ok"] is True


def test_punish_same_day_cached_not_recalled():
    g = DispositionGuard(simulation=True)
    api = _api_with_punish(["6770"])
    g.get_disposition_set(api)
    g.get_disposition_set(api)
    g.get_disposition_set(api)
    api.punish.assert_called_once()  # 同日只 call 一次


def test_punish_exception_falls_back_to_empty_with_ok_false():
    g = DispositionGuard(simulation=True)
    api = _api_with_punish([], raise_exc=True)
    codes = g.get_disposition_set(api)
    assert codes == set()
    assert g.snapshot()["ok"] is False


def test_punish_empty_list_is_valid():
    """空清單也算成功載入（市場上真的沒處置股）。"""
    g = DispositionGuard(simulation=True)
    api = _api_with_punish([])
    g.get_disposition_set(api)
    assert g.snapshot()["ok"] is True
    assert g.snapshot()["count"] == 0


# ── symbol normalization ─────────────────────────────────────────

def test_normalize_strips_tw_suffix():
    assert DispositionGuard._normalize("6770.TW") == "6770"
    assert DispositionGuard._normalize("6223.TWO") == "6223"
    assert DispositionGuard._normalize("6770") == "6770"


def test_is_disposed_handles_tw_suffix():
    g = DispositionGuard(simulation=True)
    api = _api_with_punish(["6770"])
    assert g.is_disposed(api, "6770.TW") is True
    assert g.is_disposed(api, "6770") is True
    assert g.is_disposed(api, "2330.TW") is False


# ── ensure_sellable: 非處置股 ─────────────────────────────────────────

def test_ensure_sellable_non_disposed_passes():
    g = DispositionGuard(simulation=True)
    api = _api_with_punish(["6770"])
    ok, reason = g.ensure_sellable(api, "2330.TW", 1000)
    assert ok is True
    assert reason == ""


# ── ensure_sellable: sim 處置股一律 skip ─────────────────────────────

def test_ensure_sellable_sim_disposed_blocks():
    g = DispositionGuard(simulation=True)
    api = _api_with_punish(["6770"])
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000)
    assert ok is False
    assert "disposition_sim_noop" in reason
    api.reserve_stock.assert_not_called()  # sim 不該真送


# ── ensure_sellable: prod ─────────────────────────────────────────

def _prod_guard_with_codes(codes):
    g = DispositionGuard(simulation=False)
    api = _api_with_punish(codes)
    g.get_disposition_set(api)  # 預載
    return g, api


def test_ensure_sellable_prod_list_not_loaded_blocks():
    """prod 若 punish() 從沒成功 → 保守擋掉處置股 SELL。"""
    g = DispositionGuard(simulation=False)
    api = _api_with_punish([], raise_exc=True)
    g.get_disposition_set(api)
    # _ok=False，但 codes 是空的，故下面測一個假設存在的（codes 不包含 → 直接放行不到 list_not_loaded）
    # 改：手動注入 codes 模擬「曾載入但目前失敗」的情境
    g._codes = {"6770"}
    g._ok = False
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000)
    assert ok is False
    assert "disposition_list_not_loaded" in reason


def test_ensure_sellable_prod_out_of_hours_blocks():
    g, api = _prod_guard_with_codes(["6770"])
    # 強制 now 是 07:00（在服務時段外）
    now = datetime.datetime(2026, 6, 11, 7, 0, 0)
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000, now=now)
    assert ok is False
    assert "disposition_out_of_hours" in reason
    api.reserve_stock.assert_not_called()


def test_ensure_sellable_prod_in_hours_reserve_success():
    g, api = _prod_guard_with_codes(["6770"])
    # mock Contracts.Stocks["6770"]
    api.Contracts.Stocks.__getitem__.return_value = MagicMock(name="contract")
    api.reserve_stock.return_value = FakeReserveResponse(status=True)
    now = datetime.datetime(2026, 6, 11, 10, 0, 0)
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000, now=now)
    assert ok is True
    assert reason == ""
    api.reserve_stock.assert_called_once()


def test_ensure_sellable_prod_reserve_status_false_blocks():
    g, api = _prod_guard_with_codes(["6770"])
    api.Contracts.Stocks.__getitem__.return_value = MagicMock(name="contract")
    api.reserve_stock.return_value = FakeReserveResponse(status=False, info="quota exceeded")
    now = datetime.datetime(2026, 6, 11, 10, 0, 0)
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000, now=now)
    assert ok is False
    assert "reserve_stock_status_false" in reason


def test_ensure_sellable_prod_reserve_exception_blocks():
    g, api = _prod_guard_with_codes(["6770"])
    api.Contracts.Stocks.__getitem__.return_value = MagicMock(name="contract")
    api.reserve_stock.side_effect = TimeoutError("api down")
    now = datetime.datetime(2026, 6, 11, 10, 0, 0)
    ok, reason = g.ensure_sellable(api, "6770.TW", 1000, now=now)
    assert ok is False
    assert "reserve_stock_exception" in reason
    assert "TimeoutError" in reason


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
    now = datetime.datetime(2026, 6, 11, hh, mm, 0)
    assert DispositionGuard._in_reserve_hours(now) is exp


# ── 每日 telegram 去重 ─────────────────────────────────────────

def test_should_send_daily_telegram_once_per_day():
    g = DispositionGuard(simulation=True)
    assert g.should_send_daily_telegram() is True
    assert g.should_send_daily_telegram() is False  # 同日第二次
    assert g.should_send_daily_telegram() is False


# ── snapshot 不觸發 refresh ─────────────────────────────────────────

def test_snapshot_does_not_call_punish():
    g = DispositionGuard(simulation=True)
    api = _api_with_punish(["6770"])
    snap = g.snapshot()
    assert snap == {"date": None, "ok": False, "count": 0, "codes": []}
    api.punish.assert_not_called()


# ── is_disposed 用於 BUY 路徑 ─────────────────────────────────────────

def test_is_disposed_for_buy_path():
    """sinopac.py submit() 用 is_disposed 擋 sim BUY 處置股；確認可以單獨呼叫。"""
    g = DispositionGuard(simulation=True)
    api = _api_with_punish(["6770"])
    assert g.is_disposed(api, "6770.TW") is True
    assert g.is_disposed(api, "6770") is True
    assert g.is_disposed(api, "2330.TW") is False
    api.punish.assert_called_once()  # is_disposed 會觸發 refresh
