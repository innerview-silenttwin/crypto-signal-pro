"""Smoke test：SignalAggregator 端到端契約。

驗 analyze() 與 generate_signals() 對合成資料能跑完，回傳的 AggregatedSignal 結構合法。
**不驗** 方向、信心度數值。
"""

import pandas as pd
import pytest

from signals.aggregator import (
    SignalAggregator,
    AggregatedSignal,
    MarketType,
)
from indicators.base import IndicatorSignal, SignalType


@pytest.mark.parametrize(
    "market_type",
    [MarketType.CRYPTO, MarketType.STOCK, MarketType.FUTURES],
)
def test_aggregator_runs_end_to_end(ohlcv_df, market_type):
    """各市場類型都能在 200 根合成資料上跑完完整 pipeline。"""
    agg = SignalAggregator(market_type=market_type)
    df = agg.calculate_all(ohlcv_df.copy())
    sig = agg.generate_signals(df, symbol="TEST", timeframe="1d")

    assert isinstance(sig, AggregatedSignal)
    assert sig.symbol == "TEST"
    assert sig.timeframe == "1d"
    assert sig.direction in ("BUY", "SELL", "NEUTRAL")
    assert 0.0 <= sig.confidence <= 200.0  # 分數總和上限會隨指標數變，給寬鬆檢查
    assert 0.0 <= sig.buy_score
    assert 0.0 <= sig.sell_score
    assert sig.signal_level  # 必有等級字串
    assert sig.price > 0


def test_aggregator_buckets_signals_correctly(ohlcv_df):
    """每個 IndicatorSignal 都必須落在 buy/sell/neutral 三個 bucket 之一，
    且 buy_score/sell_score 是該 bucket 內所有 signal.score 的總和。"""
    agg = SignalAggregator(market_type=MarketType.STOCK)
    df = agg.calculate_all(ohlcv_df.copy())
    sig = agg.generate_signals(df, symbol="2330.TW", timeframe="1d")

    # 沒有重複歸類
    all_in_buckets = sig.buy_signals + sig.sell_signals + sig.neutral_signals
    assert len(all_in_buckets) == len(set(id(s) for s in all_in_buckets))

    # bucket 與 SignalType 對應
    for s in sig.buy_signals:
        assert s.signal_type in (SignalType.BUY, SignalType.STRONG_BUY)
    for s in sig.sell_signals:
        assert s.signal_type in (SignalType.SELL, SignalType.STRONG_SELL)
    for s in sig.neutral_signals:
        assert s.signal_type == SignalType.NEUTRAL

    # score 加總一致（容許浮點誤差）
    buy_sum = sum(s.score for s in sig.buy_signals)
    sell_sum = sum(s.score for s in sig.sell_signals)
    assert abs(sig.buy_score - buy_sum) < 1e-6
    assert abs(sig.sell_score - sell_sum) < 1e-6


def test_aggregator_analyze_without_layers(ohlcv_df):
    """analyze() 不傳 layers 時應等同 calculate_all + generate_signals。"""
    agg = SignalAggregator(market_type=MarketType.CRYPTO)
    sig = agg.analyze(ohlcv_df.copy(), symbol="BTC/USDT", timeframe="1d")
    assert isinstance(sig, AggregatedSignal)
    assert sig.symbol == "BTC/USDT"
    assert sig.layer_modifiers == []
    assert sig.raw_buy_score == 0.0  # 沒過 layer，不會 set
    assert sig.raw_sell_score == 0.0


def test_aggregator_analyze_with_layers_runs(ohlcv_df):
    """analyze() 接 layers 列表時不應 crash，會記錄 modifiers。"""
    from layers.regime import RegimeLayer
    agg = SignalAggregator(market_type=MarketType.STOCK)
    layers = [RegimeLayer(enabled=True)]
    sig = agg.analyze(ohlcv_df.copy(), symbol="2330.TW", timeframe="1d", layers=layers)

    assert isinstance(sig, AggregatedSignal)
    # raw_score 在 layer pipeline 開始時會被設定（即使 layer inactive 也會設）
    assert sig.raw_buy_score >= 0
    assert sig.raw_sell_score >= 0
    # 分數會被 clamp 到 [0, 100]
    assert 0 <= sig.buy_score <= 100
    assert 0 <= sig.sell_score <= 100


def test_aggregator_handles_custom_weights(ohlcv_df):
    """自訂 weights 應該被使用。"""
    custom = {"rsi": 30.0, "macd": 5.0}
    agg = SignalAggregator(market_type=MarketType.CRYPTO, weights=custom)
    # 找到 RSI / MACD 指標實例驗證 max_score
    by_name = {ind.name: ind for ind in agg.indicators}
    if "RSI" in by_name:
        assert by_name["RSI"].max_score == 30.0
    # MACD 指標可能叫 "MACD" 或別的——只要找得到，就驗
    if "MACD" in by_name:
        assert by_name["MACD"].max_score == 5.0
