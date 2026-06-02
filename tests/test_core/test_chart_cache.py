"""鎖住 1d chart cache 的「不過期不回傳」invariant。

Bug 2026-06-03：6214（非自選/非超選）走勢圖只顯示到 5/29，原因是
B1 分支看到 chart_cache 還在就直接回，沒檢查末根 K 是否為最近一個
已收盤交易日 → 過時快取永遠卡死。

下方測試固定 latest_closed_tw_trading_day() 之後驗證
_daily_cache_is_fresh() 對各種末根日期的判定。
"""

from datetime import datetime, timezone

import pytest
import pytz

import main as backend_main


def _ts(date_str: str) -> int:
    """ISO YYYY-MM-DD → unix seconds (UTC 0:00)。對應 CSV/TWSE 路徑（naive pd.Timestamp）。"""
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _ts_tpe(date_str: str) -> int:
    """ISO YYYY-MM-DD → unix seconds (Asia/Taipei 0:00)。對應 yfinance 路徑（tz-aware）。"""
    tpe = pytz.timezone("Asia/Taipei")
    return int(tpe.localize(datetime.strptime(date_str, "%Y-%m-%d")).timestamp())


@pytest.fixture
def latest_today(monkeypatch):
    monkeypatch.setattr(backend_main, "latest_closed_tw_trading_day", lambda: "2026-06-02")


def test_stale_cache_is_not_fresh(latest_today):
    cached = {"candles": [{"time": _ts("2026-05-29"), "close": 131.0}]}
    assert backend_main._daily_cache_is_fresh(cached) is False


def test_cache_at_expected_close_is_fresh(latest_today):
    cached = {"candles": [{"time": _ts("2026-06-02"), "close": 158.0}]}
    assert backend_main._daily_cache_is_fresh(cached) is True


def test_yfinance_taipei_midnight_encoding_is_fresh(latest_today):
    # 防呆：yfinance 路徑寫入 cache 時用 tz-aware Asia/Taipei midnight，
    # 對應的 unix seconds 落在 UTC 16:00 前一日。helper 必須在 Taipei tz
    # 讀回才能拿到正確日期；用 UTC 讀回會少一天，把今日 K 誤判為昨日。
    cached = {"candles": [{"time": _ts_tpe("2026-06-02"), "close": 158.0}]}
    assert backend_main._daily_cache_is_fresh(cached) is True


def test_cache_ahead_of_expected_close_is_fresh(latest_today):
    # 邊界：cache 末根比預期還新（例如盤中 14:30 前已收進今天的盤中 K）→ 視為 fresh
    cached = {"candles": [{"time": _ts("2026-06-03"), "close": 160.0}]}
    assert backend_main._daily_cache_is_fresh(cached) is True


def test_empty_cache_is_not_fresh(latest_today):
    assert backend_main._daily_cache_is_fresh({"candles": []}) is False
    assert backend_main._daily_cache_is_fresh({}) is False


def test_cache_missing_time_field_is_not_fresh(latest_today):
    cached = {"candles": [{"close": 158.0}]}  # 缺 time
    assert backend_main._daily_cache_is_fresh(cached) is False
