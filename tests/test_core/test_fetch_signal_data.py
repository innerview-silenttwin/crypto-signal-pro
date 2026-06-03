"""sector_auto_trader.fetch_signal_data 的 freshness 守則測試。

底線（用戶 2026-06-03 指示）：
- 交易絕對不能用 stale 資料判斷觸發
- 寧可 skip 該輪也不能用舊 CSV

這些測試確保 fetch_signal_data 在 quote_provider 失敗/回 stale 時返回 None
（不再 fallback 本地 CSV）。
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest
import pytz

from sector_auto_trader import fetch_signal_data, _price_cache, _expected_latest_trading_day_date


@pytest.fixture(autouse=True)
def clear_price_cache():
    """每個測試前後清掉記憶體 cache，避免互相干擾。"""
    _price_cache.clear()
    yield
    _price_cache.clear()


def _make_df(last_date, rows=100):
    """造一份合法 OHLCV DataFrame，最後一根 K 是 last_date。

    用 calendar day（'D'）而非 business day（'B'），避免 freq='B' 在跨假期時
    回傳 periods-1 筆造成 length mismatch。fetch_signal_data 的 freshness 判斷
    只看 last_date，跨假期不重要。
    """
    idx = pd.date_range(end=pd.Timestamp(last_date), periods=rows, freq="D")
    return pd.DataFrame({
        "open":   [100.0] * rows,
        "high":   [102.0] * rows,
        "low":    [98.0] * rows,
        "close":  [101.0] * rows,
        "volume": [1_000_000] * rows,
    }, index=idx)


# ─────────────────────────────────────────────────────
# 1. quote_provider 成功 + 新鮮 → 正常回 df
# ─────────────────────────────────────────────────────

def test_returns_df_when_provider_returns_fresh_data():
    expected = _expected_latest_trading_day_date()
    fresh_df = _make_df(expected)

    class FakeProvider:
        def get_history(self, sym, period_days=250, interval="1d"):
            return fresh_df

    with patch("sector_auto_trader.get_quote_provider", return_value=FakeProvider()):
        result = fetch_signal_data("2330.TW")
        assert result is not None
        assert len(result) > 0


# ─────────────────────────────────────────────────────
# 2. quote_provider 回 stale 資料 → 必須 skip (return None)
# ─────────────────────────────────────────────────────

def test_skip_when_provider_returns_stale_data():
    """quote provider 回的 last_date 比上個交易日舊 → 必須 return None，不可 fallback CSV。"""
    expected = _expected_latest_trading_day_date()
    stale_date = expected - timedelta(days=10)
    stale_df = _make_df(stale_date)

    class FakeProvider:
        def get_history(self, sym, period_days=250, interval="1d"):
            return stale_df

    with patch("sector_auto_trader.get_quote_provider", return_value=FakeProvider()):
        result = fetch_signal_data("2330.TW")
        assert result is None, "stale 資料必須 return None 避免觸發舊資料交易"


def test_skip_when_provider_returns_friday_data_on_tuesday():
    """模擬上週五 CSV 殘留情境（6/3 觀察的真實 bug）"""
    # 假設今天是 2026-06-03 週二，期望最新 = 6/2 週一
    # 但 provider 給了 5/29 上週五的資料（10 天前）
    expected = _expected_latest_trading_day_date()
    week_old = expected - timedelta(days=5)
    stale_df = _make_df(week_old)

    class FakeProvider:
        def get_history(self, sym, period_days=250, interval="1d"):
            return stale_df

    with patch("sector_auto_trader.get_quote_provider", return_value=FakeProvider()):
        result = fetch_signal_data("2330.TW")
        assert result is None


# ─────────────────────────────────────────────────────
# 3. 今日 partial snapshot 允許（盤中 last_date == today）
# ─────────────────────────────────────────────────────

def test_accepts_today_partial_snapshot():
    """盤中 yfinance 回的 last_date == today（即使 > expected_latest）也應接受。"""
    today = datetime.now(pytz.timezone("Asia/Taipei")).date()
    today_df = _make_df(today)

    class FakeProvider:
        def get_history(self, sym, period_days=250, interval="1d"):
            return today_df

    with patch("sector_auto_trader.get_quote_provider", return_value=FakeProvider()):
        result = fetch_signal_data("2330.TW")
        assert result is not None


# ─────────────────────────────────────────────────────
# 4. provider 失敗 → 必須 return None，不可 fallback CSV
# ─────────────────────────────────────────────────────

def test_skip_when_provider_returns_none():
    class FakeProvider:
        def get_history(self, sym, period_days=250, interval="1d"):
            return None

    with patch("sector_auto_trader.get_quote_provider", return_value=FakeProvider()):
        result = fetch_signal_data("2330.TW")
        assert result is None


def test_skip_when_provider_returns_empty():
    class FakeProvider:
        def get_history(self, sym, period_days=250, interval="1d"):
            return pd.DataFrame()

    with patch("sector_auto_trader.get_quote_provider", return_value=FakeProvider()):
        result = fetch_signal_data("2330.TW")
        assert result is None


def test_skip_when_provider_returns_too_few_rows():
    """資料 < 50 筆視同失敗，skip。"""
    short_df = _make_df(_expected_latest_trading_day_date(), rows=30)

    class FakeProvider:
        def get_history(self, sym, period_days=250, interval="1d"):
            return short_df

    with patch("sector_auto_trader.get_quote_provider", return_value=FakeProvider()):
        result = fetch_signal_data("2330.TW")
        assert result is None


def test_skip_when_provider_raises():
    """provider 拋例外 → 必須 return None，不可 fallback CSV。"""
    class FakeProvider:
        def get_history(self, sym, period_days=250, interval="1d"):
            raise ConnectionError("network down")

    with patch("sector_auto_trader.get_quote_provider", return_value=FakeProvider()):
        result = fetch_signal_data("2330.TW")
        assert result is None


# ─────────────────────────────────────────────────────
# 5. memory cache hit 跳過 provider
# ─────────────────────────────────────────────────────

def test_memory_cache_hit_skips_provider_call():
    expected = _expected_latest_trading_day_date()
    cached_df = _make_df(expected)
    _price_cache["2330.TW"] = {"df": cached_df, "time": __import__("time").time()}

    call_count = {"n": 0}

    class FakeProvider:
        def get_history(self, sym, period_days=250, interval="1d"):
            call_count["n"] += 1
            return _make_df(expected)

    with patch("sector_auto_trader.get_quote_provider", return_value=FakeProvider()):
        result = fetch_signal_data("2330.TW")
        assert result is cached_df, "120s 內 cache hit 應直接回 cached，不打 provider"
        assert call_count["n"] == 0
