"""/api/chart 的 integration test（A5b deferred 補測）。

重點：
1. ccxt 路徑（market='crypto'）的 lazy import 在 request 時能 resolve。
2. **double-close regression**：無論成功或 fetch 例外，exchange.close() 只被呼叫一次
   （修正前 except 分支會二度 close 已關閉的 exchange）。

不打真網路：mock 掉 ccxt_async.binance 與 main.fetch_ohlcv_async。
用 asyncio.run() 驅動 coroutine，避免引入 pytest-asyncio 依賴。
"""

import asyncio
import sys
import types

import pandas as pd
import pytest

import api.stocks as stocks


class _FakeExchange:
    """記錄 close() 被呼叫幾次的假 ccxt exchange。"""

    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1


@pytest.fixture
def fake_main(monkeypatch):
    """注入假的 main module，控制 fetch_ohlcv_async 行為。

    保存並還原 sys.modules['main']，避免污染其他 test。
    """
    saved = sys.modules.get("main")
    fake = types.ModuleType("main")

    def _get_tw_chart_data(symbol, timeframe, limit):
        return None  # 本測試不走台股路徑

    fake.get_tw_chart_data = _get_tw_chart_data
    fake.fetch_ohlcv_async = None  # 由各 test 指定
    sys.modules["main"] = fake
    yield fake
    if saved is not None:
        sys.modules["main"] = saved
    else:
        del sys.modules["main"]


def _patch_exchange(monkeypatch):
    """讓 ccxt_async.binance() 回傳同一個可檢查的 _FakeExchange。"""
    ex = _FakeExchange()
    monkeypatch.setattr(stocks.ccxt_async, "binance", lambda *a, **k: ex)
    return ex


def test_chart_crypto_success_closes_once(fake_main, monkeypatch):
    ex = _patch_exchange(monkeypatch)

    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    df = pd.DataFrame(
        {"open": [1, 2, 3], "high": [2, 3, 4], "low": [0.5, 1, 2],
         "close": [1.5, 2.5, 3.5], "volume": [10, 20, 30]},
        index=idx,
    )

    async def _fetch(exchange, symbol, timeframe, limit):
        return df

    fake_main.fetch_ohlcv_async = _fetch

    res = asyncio.run(
        stocks.get_chart_data(symbol="BTC/USDT", timeframe="1d", market="crypto")
    )

    assert res["data_source"] == "ccxt"
    assert len(res["candles"]) == 3
    assert ex.close_calls == 1  # 成功路徑只 close 一次


class _BadFrame:
    """非 None 但在組 candle（iterrows）時拋例外，模擬 fetch 成功後的下游錯誤。"""

    def iterrows(self):
        raise RuntimeError("boom during candle build")


def test_chart_crypto_error_after_fetch_closes_once(fake_main, monkeypatch):
    """double-close regression：fetch 成功後下游拋例外，exchange 仍只 close 一次。

    修正前：try 內 fetch 後先 close（1），組 candle 拋例外進 except 再 close（2）= double close。
    修正後：close 移到 finally，無論如何只 close 一次。
    """
    ex = _patch_exchange(monkeypatch)

    async def _fetch(exchange, symbol, timeframe, limit):
        return _BadFrame()  # 非 None，會進入組 candle 分支後拋例外

    fake_main.fetch_ohlcv_async = _fetch

    res = asyncio.run(
        stocks.get_chart_data(symbol="BTC/USDT", timeframe="1d", market="crypto")
    )

    assert res["candles"] == []
    assert res["data_source"] is None
    assert ex.close_calls == 1  # 關鍵斷言：修正前此處為 2
