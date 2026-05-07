"""測試 RiskGate 規則 — 每條規則一個 case + TSMC below_min_lot 真實情境。"""

import time
from unittest import mock

import pytest

from brokers.risk_gate import RiskConfig, RiskGate
from brokers.state_store import BrokerStateStore


@pytest.fixture
def store(tmp_path):
    return BrokerStateStore(str(tmp_path / "broker_state.json"))


def _make_gate(store, *, equity=2_000_000.0, balance=2_000_000.0, position=None,
               cfg=None, clock=None, holding=None):
    """holding 表示既有持倉 {qty, avg_price}（給 SELL 測試用）"""
    cfg = cfg or RiskConfig(enforce_market_hours=False)  # 測試預設不檢查時段
    return RiskGate(
        config=cfg,
        state_store=store,
        equity_provider=lambda sid: (equity, balance),
        position_provider=lambda sid, sym: holding or (position or {}),
        initial_balance_provider=lambda sid: 2_000_000.0,
        clock=clock or (lambda: 1000.0),
    )


# ── BUY 規則 ──

def test_buy_below_min_lot_tsmc_real_scenario(store):
    """TSMC 1845元 × 0.10 ratio × 200萬 / 1845 = 108 股 < 1 張 → below_min_lot."""
    gate = _make_gate(store)
    d = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                   action="BUY", qty_shares=108, limit_price=1845.0)
    assert d.ok is False
    assert d.reason == "below_min_lot"
    # needed_twd 應該大致為 1 張 + 手續費
    assert d.needed_twd > 1845 * 1000


def test_buy_over_max_order_amount(store):
    cfg = RiskConfig(enforce_market_hours=False, max_order_amount_twd=100_000.0)
    gate = _make_gate(store, cfg=cfg)
    # 1 張 × 200元 = 20萬，超過上限 10萬
    d = gate.allow(sector_id="semiconductor", symbol="2317.TW",
                   action="BUY", qty_shares=1000, limit_price=200.0)
    assert d.ok is False
    assert d.reason.startswith("over_max_order")


def test_buy_over_position_pct(store):
    """已有大持倉，再買會超過 15% 占比上限。"""
    cfg = RiskConfig(enforce_market_hours=False, max_position_pct_per_symbol=15.0,
                     max_order_amount_twd=10_000_000.0)
    # equity 200萬；既有 2317.TW 持倉 1000 股 × 100 = 10萬（5%）
    # 再買 1000 股 × 200 = 20萬 → 加總 30萬 / 200萬 = 15% 剛好；多 1 股就會超
    # 設定 1 張 250元 = 25萬 → (10萬 + 25萬) / 200萬 = 17.5% 超過
    gate = _make_gate(
        store, cfg=cfg,
        position={"qty": 1000, "avg_price": 100.0},
    )
    d = gate.allow(sector_id="semiconductor", symbol="2317.TW",
                   action="BUY", qty_shares=1000, limit_price=250.0)
    assert d.ok is False
    assert "over_position_pct" in d.reason


def test_buy_pending_order_blocks(store):
    from brokers.state_store import PendingOrder
    gate = _make_gate(store)
    store.add_pending(PendingOrder(
        client_order_id="x", sector_id="semiconductor", symbol="2330.TW",
        action="BUY", qty_shares=1000, limit_price=900.0, submitted_at=time.time(),
    ))
    d = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                   action="BUY", qty_shares=1000, limit_price=900.0)
    assert d.ok is False
    assert d.reason == "pending_exists"


def test_buy_cooldown_after_buy_blocks(store):
    cfg = RiskConfig(enforce_market_hours=False, cooldown_minutes_after_buy=30)
    gate = _make_gate(store, cfg=cfg, clock=lambda: 1000.0)
    store.set_cooldown("semiconductor", "2330.TW", "BUY", expires_at=1000 + 1500)
    d = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                   action="BUY", qty_shares=1000, limit_price=900.0)
    assert d.ok is False
    assert d.reason.startswith("cooldown_buy:")


def test_buy_cooldown_after_sell_blocks(store):
    cfg = RiskConfig(enforce_market_hours=False, cooldown_minutes_after_sell=60)
    gate = _make_gate(store, cfg=cfg, clock=lambda: 1000.0)
    store.set_cooldown("semiconductor", "2330.TW", "SELL", expires_at=1000 + 3000)
    d = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                   action="BUY", qty_shares=1000, limit_price=900.0)
    assert d.ok is False
    assert d.reason.startswith("cooldown_after_sell:")


def test_buy_max_daily_orders_total(store):
    from brokers.state_store import today_tw
    cfg = RiskConfig(enforce_market_hours=False, max_daily_orders_total=2,
                     max_order_amount_twd=10_000_000.0)  # 暫高，讓金額不擋
    gate = _make_gate(store, cfg=cfg)
    today = today_tw()
    store.incr_order_count(today, "semiconductor")
    store.incr_order_count(today, "electronics")
    d = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                   action="BUY", qty_shares=1000, limit_price=900.0)
    assert d.ok is False
    assert d.reason == "max_daily_orders_total"


def test_buy_kill_switch_blocks_everything(store):
    from brokers.state_store import today_tw
    gate = _make_gate(store)
    store.lock_today(today_tw(), "test_lock")
    d = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                   action="BUY", qty_shares=1000, limit_price=900.0)
    assert d.ok is False
    assert d.reason.startswith("daily_locked:")


def test_buy_market_closed(store):
    cfg = RiskConfig(enforce_market_hours=True)  # 開啟時段檢查
    # 因為 sniper test 不能假設真的在時段內，我們 monkeypatch is_orderable_now
    gate = _make_gate(store, cfg=cfg)
    with mock.patch("brokers.risk_gate.is_orderable_now", return_value=False):
        d = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                       action="BUY", qty_shares=1000, limit_price=900.0)
    assert d.ok is False
    assert d.reason == "market_closed"


# ── SELL 規則 ──

def test_sell_no_position_blocks(store):
    gate = _make_gate(store, holding={})
    d = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                   action="SELL", qty_shares=1000, limit_price=900.0)
    assert d.ok is False
    assert d.reason == "no_position"


def test_sell_ex_div_freeze_only_blocks_auto_stop(store):
    gate = _make_gate(store, holding={"qty": 1000, "avg_price": 800})
    with mock.patch("brokers.risk_gate.is_within_ex_div_freeze", return_value=True):
        # 自動停損 → 擋
        d_stop = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                            action="SELL", qty_shares=1000, limit_price=900.0,
                            is_auto_stop=True)
        assert d_stop.ok is False and d_stop.reason == "ex_div_freeze"

        # 標準信號賣（非 auto_stop）→ 放行
        d_normal = gate.allow(sector_id="semiconductor", symbol="2330.TW",
                              action="SELL", qty_shares=1000, limit_price=900.0,
                              is_auto_stop=False)
        assert d_normal.ok is True


# ── record_success 設置 cooldown ──

def test_record_success_sets_cooldown(store):
    cfg = RiskConfig(enforce_market_hours=False,
                     cooldown_minutes_after_buy=30,
                     cooldown_minutes_after_sell=60)
    clock_value = [10000.0]
    gate = _make_gate(store, cfg=cfg, clock=lambda: clock_value[0])

    gate.record_success("semiconductor", "2330.TW", "BUY")
    assert store.in_cooldown("semiconductor", "2330.TW", "BUY", clock_value[0]) is True
    assert store.in_cooldown("semiconductor", "2330.TW", "BUY", clock_value[0] + 30 * 60 - 1) is True
    assert store.in_cooldown("semiconductor", "2330.TW", "BUY", clock_value[0] + 30 * 60 + 1) is False

    gate.record_success("electronics", "2317.TW", "SELL")
    assert store.in_cooldown("electronics", "2317.TW", "SELL", clock_value[0] + 60 * 60 - 1) is True


def test_kill_switch_triggers_on_loss_threshold(store):
    cfg = RiskConfig(enforce_market_hours=False, max_daily_loss_pct=2.0)
    gate = _make_gate(store, cfg=cfg)
    # initial_balance = 200萬；2% = 4萬
    gate.maybe_trigger_kill_switch("semiconductor", -30_000)  # 1.5%
    from brokers.state_store import today_tw
    assert store.is_locked_today(today_tw()) is False
    gate.maybe_trigger_kill_switch("semiconductor", -15_000)  # 累計 4.5萬 = 2.25%
    assert store.is_locked_today(today_tw()) is True
