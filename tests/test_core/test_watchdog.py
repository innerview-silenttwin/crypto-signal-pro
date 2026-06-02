"""silent-watchdog 的單元測試。

Bug 2026-06-02 教訓：原 _scan_account_last_trades 用 hist[-1] 假設
history 是 oldest-first，但 sector_trader 用 insert(0, ...) → list 是
newest-first，導致 watchdog 連 2 天誤報全部帳本靜默 50+ 天。

這些測試確保不論 history 順序如何，都能拿到「時間最大」的那筆。
"""

import json
import os

import pytest

from main import _scan_account_last_trades


@pytest.fixture
def fake_proj(tmp_path):
    """建立假的 project root 結構：含 data/btc_trading_account.json + data/sector_accounts/*"""
    (tmp_path / "data" / "sector_accounts").mkdir(parents=True)
    return tmp_path


def _write_account(path, history_list):
    with open(path, "w") as f:
        json.dump({"history": history_list}, f)


def _entry(time_str, symbol="2330.TW", trade_type="BUY"):
    return {"time": time_str, "symbol": symbol, "type": trade_type}


# ─────────────────────────────────────────────────────
# 核心 bug 防護：list 順序不論 newest-first / oldest-first 都要拿到最新
# ─────────────────────────────────────────────────────

def test_picks_latest_even_when_list_is_newest_first(fake_proj):
    """sector_trader 實際格式：insert(0) 寫紀錄 → list[0] 是最新、list[-1] 是最舊。"""
    p = fake_proj / "data" / "sector_accounts" / "semiconductor_account.json"
    _write_account(p, [
        _entry("2026-06-01 12:00:00"),  # 最新（list[0]）
        _entry("2026-04-15 10:00:00"),
        _entry("2026-03-01 09:00:00"),  # 最舊（list[-1]）— 之前 bug 拿這個
    ])
    results = _scan_account_last_trades(str(fake_proj))
    for label, dt in results:
        if label == "semiconductor":
            assert dt is not None
            assert dt.strftime("%Y-%m-%d %H:%M:%S") == "2026-06-01 12:00:00"
            return
    pytest.fail("semiconductor account not scanned")


def test_picks_latest_when_list_is_oldest_first(fake_proj):
    """防禦：未來若改成 oldest-first 寫紀錄，仍能正確找到最新。"""
    p = fake_proj / "data" / "sector_accounts" / "electronics_account.json"
    _write_account(p, [
        _entry("2026-03-01 09:00:00"),  # 最舊
        _entry("2026-04-15 10:00:00"),
        _entry("2026-06-01 12:00:00"),  # 最新（list[-1]）
    ])
    results = _scan_account_last_trades(str(fake_proj))
    for label, dt in results:
        if label == "electronics":
            assert dt is not None
            assert dt.strftime("%Y-%m-%d %H:%M:%S") == "2026-06-01 12:00:00"
            return
    pytest.fail("electronics account not scanned")


def test_picks_latest_when_list_is_unsorted(fake_proj):
    """防禦：list 順序隨機時，仍能正確找到最新。"""
    p = fake_proj / "data" / "sector_accounts" / "other_account.json"
    _write_account(p, [
        _entry("2026-04-15 10:00:00"),
        _entry("2026-06-01 12:00:00"),  # 最新（list[1]）
        _entry("2026-03-01 09:00:00"),
    ])
    results = _scan_account_last_trades(str(fake_proj))
    by_label = {label: dt for label, dt in results}
    assert by_label["other"] is not None
    assert by_label["other"].strftime("%Y-%m-%d %H:%M:%S") == "2026-06-01 12:00:00"


# ─────────────────────────────────────────────────────
# 邊界 case
# ─────────────────────────────────────────────────────

def test_empty_history_returns_none(fake_proj):
    p = fake_proj / "data" / "sector_accounts" / "finance_account.json"
    _write_account(p, [])
    results = _scan_account_last_trades(str(fake_proj))
    by_label = {label: dt for label, dt in results}
    assert by_label["finance"] is None


def test_missing_time_field_skipped(fake_proj):
    """entry 沒有 time 欄位的條目應被忽略，不影響其他條目。"""
    p = fake_proj / "data" / "sector_accounts" / "traditional_account.json"
    _write_account(p, [
        {"symbol": "1101.TW", "type": "BUY"},  # 沒有 time
        _entry("2026-05-15 10:00:00"),
        {"time": ""},  # 空字串 time
    ])
    results = _scan_account_last_trades(str(fake_proj))
    by_label = {label: dt for label, dt in results}
    assert by_label["traditional"] is not None
    assert by_label["traditional"].strftime("%Y-%m-%d %H:%M:%S") == "2026-05-15 10:00:00"


def test_no_account_files_returns_empty(tmp_path):
    """data dir 完全不存在時不該 crash，回空 list。"""
    results = _scan_account_last_trades(str(tmp_path))
    assert results == []


def test_btc_account_also_scanned(fake_proj):
    """BTC 帳本也要包含在掃描範圍內。"""
    btc_path = fake_proj / "data" / "btc_trading_account.json"
    _write_account(btc_path, [_entry("2026-06-02 00:47:00", symbol="BTC/USDT_S1")])
    results = _scan_account_last_trades(str(fake_proj))
    by_label = {label: dt for label, dt in results}
    assert "BTC自動" in by_label
    assert by_label["BTC自動"].strftime("%Y-%m-%d %H:%M:%S") == "2026-06-02 00:47:00"


def test_multiple_accounts_all_scanned(fake_proj):
    """同時掃多個 sector + BTC，各自獨立回傳。"""
    _write_account(fake_proj / "data" / "btc_trading_account.json",
                   [_entry("2026-06-02 00:00:00")])
    _write_account(fake_proj / "data" / "sector_accounts" / "semiconductor_account.json",
                   [_entry("2026-06-01 12:00:00")])
    _write_account(fake_proj / "data" / "sector_accounts" / "electronics_account.json",
                   [_entry("2026-05-30 10:00:00")])
    results = _scan_account_last_trades(str(fake_proj))
    labels = {label for label, _ in results}
    assert labels == {"BTC自動", "semiconductor", "electronics"}


def test_bak_files_excluded(fake_proj):
    """.bak 檔不該被掃進來。"""
    real = fake_proj / "data" / "sector_accounts" / "precision_account.json"
    bak = fake_proj / "data" / "sector_accounts" / "precision_account.json.bak.20260522"
    _write_account(real, [_entry("2026-06-01 09:00:00")])
    _write_account(bak, [_entry("2099-12-31 23:59:59")])  # 故意給未來時間，若被掃會被選中
    results = _scan_account_last_trades(str(fake_proj))
    by_label = {label: dt for label, dt in results}
    assert "precision" in by_label
    assert by_label["precision"].strftime("%Y-%m-%d %H:%M:%S") == "2026-06-01 09:00:00"
    # bak 不該以任何方式出現
    for label, _ in results:
        assert "bak" not in label.lower()
