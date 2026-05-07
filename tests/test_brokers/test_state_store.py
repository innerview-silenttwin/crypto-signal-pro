"""測試 BrokerStateStore — atomic write、cooldown、kill-switch、daily counters。"""

import json
import os
import time

from brokers.state_store import BrokerStateStore, PendingOrder, today_tw


def _new_store(tmp_path):
    return BrokerStateStore(str(tmp_path / "broker_state.json"))


def test_pending_orders_round_trip(tmp_path):
    store = _new_store(tmp_path)
    p = PendingOrder(
        client_order_id="abc", sector_id="semiconductor", symbol="2330.TW",
        action="BUY", qty_shares=1000, limit_price=900.0, submitted_at=time.time(),
    )
    store.add_pending(p)
    assert store.has_pending_for_symbol("semiconductor", "2330.TW") is True
    assert store.has_pending_for_symbol("electronics", "2330.TW") is False
    got = store.get_pending("abc")
    assert got and got.symbol == "2330.TW"
    store.remove_pending("abc")
    assert store.has_pending_for_symbol("semiconductor", "2330.TW") is False


def test_cooldown_and_remaining(tmp_path):
    store = _new_store(tmp_path)
    now = 1000.0
    store.set_cooldown("semiconductor", "2330.TW", "BUY", expires_at=now + 60)
    assert store.in_cooldown("semiconductor", "2330.TW", "BUY", now) is True
    assert store.in_cooldown("semiconductor", "2330.TW", "BUY", now + 30) is True
    assert store.in_cooldown("semiconductor", "2330.TW", "BUY", now + 60) is False
    assert store.cooldown_remaining("semiconductor", "2330.TW", "BUY", now + 30) == 30


def test_kill_switch(tmp_path):
    store = _new_store(tmp_path)
    today = today_tw()
    assert store.is_locked_today(today) is False
    store.lock_today(today, "test_loss_2.5pct")
    assert store.is_locked_today(today) is True
    assert store.get_daily_lock().reason == "test_loss_2.5pct"
    # 不同日期不算鎖
    assert store.is_locked_today("1999-01-01") is False


def test_persisted_across_reload(tmp_path):
    path = tmp_path / "broker_state.json"
    store1 = BrokerStateStore(str(path))
    store1.set_cooldown("electronics", "2317.TW", "SELL", expires_at=999999.0)
    store1.lock_today("2026-05-06", "manual_test")
    # 重啟
    store2 = BrokerStateStore(str(path))
    assert store2.in_cooldown("electronics", "2317.TW", "SELL", 999998) is True
    assert store2.is_locked_today("2026-05-06") is True


def test_atomic_write_no_partial_file(tmp_path):
    """寫檔失敗時不該留下半個檔。"""
    store = _new_store(tmp_path)
    # 使其資料夾下沒有 partial .tmp 殘留
    store.set_cooldown("semiconductor", "2330.TW", "BUY", expires_at=time.time() + 60)
    # 沒有 .tmp 殘留
    leftover = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftover == []
    # 主檔合法 JSON
    with open(tmp_path / "broker_state.json") as f:
        data = json.load(f)
    assert "cooldowns" in data


def test_daily_counters(tmp_path):
    store = _new_store(tmp_path)
    today = today_tw()
    store.incr_order_count(today, "semiconductor")
    store.incr_order_count(today, "semiconductor")
    store.incr_order_count(today, "electronics")
    assert store.get_order_count(today, "semiconductor") == 2
    assert store.get_order_count(today, "electronics") == 1
    assert store.get_order_count(today, "_total") == 3


def test_realized_pnl_accumulates(tmp_path):
    store = _new_store(tmp_path)
    today = today_tw()
    store.add_realized_pnl(today, "semiconductor", -10000)
    store.add_realized_pnl(today, "semiconductor", -5000)
    assert store.get_realized_pnl(today, "semiconductor") == -15000


# ── try_reserve_for_symbol：修補 TOCTOU race 的 atomic check-and-insert ──

def _make_pending(cid: str, sector_id="semiconductor", symbol="2330.TW"):
    return PendingOrder(
        client_order_id=cid, sector_id=sector_id, symbol=symbol,
        action="BUY", qty_shares=1000, limit_price=900.0,
        submitted_at=time.time(),
    )


def test_try_reserve_first_wins(tmp_path):
    store = _new_store(tmp_path)
    assert store.try_reserve_for_symbol(_make_pending("cid1")) is True
    # 同 sector + symbol：第二筆失敗
    assert store.try_reserve_for_symbol(_make_pending("cid2")) is False
    # 第一筆釋放後，第二筆可以
    store.remove_pending("cid1")
    assert store.try_reserve_for_symbol(_make_pending("cid2")) is True


def test_try_reserve_different_symbols_independent(tmp_path):
    store = _new_store(tmp_path)
    assert store.try_reserve_for_symbol(_make_pending("cid1", symbol="2330.TW")) is True
    assert store.try_reserve_for_symbol(_make_pending("cid2", symbol="2454.TW")) is True
    assert store.try_reserve_for_symbol(_make_pending("cid3", symbol="2330.TW")) is False


def test_try_reserve_concurrent_only_one_wins(tmp_path):
    """模擬 daemon thread + HTTP-triggered thread 同時對同 symbol 跑 reserve。

    用 50 個 thread 競爭，必須剛好一個成功。
    """
    import threading
    store = _new_store(tmp_path)
    results = []
    barrier = threading.Barrier(50)

    def worker(idx):
        barrier.wait()  # 確保所有 thread 同時起跑
        ok = store.try_reserve_for_symbol(_make_pending(f"cid{idx}"))
        results.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = sum(1 for r in results if r is True)
    assert successes == 1, f"預期剛好 1 個成功，實際 {successes}"
    # 帳本只剩 1 筆 pending
    assert len(store.list_pending()) == 1
