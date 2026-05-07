"""測試 VirtualBroker 與 SectorTradingManager broker injection 後的行為。"""

import os

import pytest

from brokers.virtual import VirtualBroker
from brokers.base import BrokerResult


def test_virtual_broker_fills_immediately():
    b = VirtualBroker()
    r = b.submit(symbol="2330.TW", action="BUY", qty_shares=1000,
                 limit_price=900.0, client_order_id="cid1",
                 sector_id="semiconductor")
    assert r.ok is True
    assert r.actual_qty == 1000
    assert r.actual_price == 900.0
    assert r.fill_status == "filled"


def test_virtual_broker_rejects_invalid():
    b = VirtualBroker()
    r = b.submit(symbol="2330.TW", action="BUY", qty_shares=0,
                 limit_price=900.0, client_order_id="cid", sector_id="x")
    assert r.ok is False
    assert r.reason == "invalid_qty_or_price"


def test_virtual_broker_other_methods_are_safe():
    b = VirtualBroker()
    assert b.cancel("any") is True
    assert b.reconcile() == []
    assert b.get_account_positions() == {}


# ── SectorTradingManager 整合測試 ──

@pytest.fixture
def isolated_manager(tmp_path, monkeypatch):
    """在 tmp 環境裡建一個 manager（避免污染真正的 sector_accounts）。"""
    # 先 import sector_trader（會觸發既有 sector_managers 全域實例）
    import sector_trader

    # 換掉 DATA_DIR 並重建 manager
    monkeypatch.setattr(sector_trader, "DATA_DIR", str(tmp_path))
    # 直接 new 一個 manager；它會用新 DATA_DIR
    mgr = sector_trader.SectorTradingManager("半導體")
    # initial_balance 應該是 200 萬
    assert mgr.state["balance"] == 2_000_000.0
    return mgr


def test_manager_buy_via_virtual_broker(isolated_manager, monkeypatch):
    """確認 broker injection 後 BUY 行為跟舊 execute_trade 等價（虛擬模式）。"""
    # 阻止 telegram 真的發送
    import notifier
    monkeypatch.setattr(notifier, "send_telegram", lambda *a, **kw: True)

    mgr = isolated_manager
    # 手動 attach 一個 fresh VirtualBroker，避免依賴內部 lazy 初始化
    mgr.attach_broker(VirtualBroker())

    initial = mgr.state["balance"]
    ok = mgr.execute_trade("2330.TW", "BUY", 900.0, "test buy", ratio=0.10)
    assert ok is True

    # ledger 已寫入
    assert "2330.TW" in mgr.state["holdings"]
    h = mgr.state["holdings"]["2330.TW"]
    assert h["qty"] > 0
    assert h["avg_price"] == 900.0
    # 餘額減少了（含手續費）
    assert mgr.state["balance"] < initial
    # history 有一筆 BUY
    assert mgr.state["history"][0]["type"] == "BUY"
    assert mgr.state["history"][0]["broker"] == "virtual"


def test_manager_sell_full_position(isolated_manager, monkeypatch):
    import notifier
    monkeypatch.setattr(notifier, "send_telegram", lambda *a, **kw: True)

    mgr = isolated_manager
    mgr.attach_broker(VirtualBroker())

    # 先買，再賣
    mgr.execute_trade("2330.TW", "BUY", 900.0, "buy", ratio=0.10)
    qty_before = mgr.state["holdings"]["2330.TW"]["qty"]
    ok = mgr.execute_trade("2330.TW", "SELL", 1000.0, "sell")
    assert ok is True
    # 全賣 → 持倉移除
    assert "2330.TW" not in mgr.state["holdings"]
    sell_log = mgr.state["history"][0]
    assert sell_log["type"] == "SELL"
    assert sell_log["qty"] == qty_before
    assert sell_log["profit"] > 0  # 從 900 賣到 1000


def test_manager_buy_blocked_by_zero_qty(isolated_manager, monkeypatch):
    """價格極高 / ratio 極小 → 算出 0 股 → 直接 False，不寫 ledger."""
    import notifier
    monkeypatch.setattr(notifier, "send_telegram", lambda *a, **kw: True)

    mgr = isolated_manager
    mgr.attach_broker(VirtualBroker())
    # ratio 0.0001 × 200萬 / 99999 = 約 2 股 → 不到 1 股以上
    ok = mgr.execute_trade("9999.TW", "BUY", 99_999_999.0, "test", ratio=0.0001)
    assert ok is False
    assert "9999.TW" not in mgr.state["holdings"]


def test_atomic_save_no_tmp_leftover(isolated_manager, monkeypatch):
    import notifier
    monkeypatch.setattr(notifier, "send_telegram", lambda *a, **kw: True)
    mgr = isolated_manager
    mgr.attach_broker(VirtualBroker())
    mgr.execute_trade("2330.TW", "BUY", 900.0, "buy", ratio=0.10)
    leftover = [f for f in os.listdir(os.path.dirname(mgr.data_file)) if f.endswith(".tmp")]
    assert leftover == []


def test_broker_field_in_history(isolated_manager, monkeypatch):
    """驗證 history 多了 broker 欄位但 schema 沒破壞既有欄位。"""
    import notifier
    monkeypatch.setattr(notifier, "send_telegram", lambda *a, **kw: True)
    mgr = isolated_manager
    mgr.attach_broker(VirtualBroker())
    mgr.execute_trade("2330.TW", "BUY", 900.0, "test", ratio=0.10)
    rec = mgr.state["history"][0]
    # 既有欄位仍存在
    for field in ("id", "time", "symbol", "name", "type", "price", "qty",
                   "cost", "signal", "balance_after"):
        assert field in rec
    # 新欄位
    assert rec["broker"] == "virtual"
