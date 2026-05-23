"""Smoke test：layers 契約。

策略：
- regime：純計算，可直接對合成 DataFrame 測
- chipflow / fundamental / sentiment：依賴外部資料（TWSE OpenAPI / RSS）
  → 用 monkeypatch 把 fetch 函式換成回傳空，驗證「資料拿不到」分支安全降級
  → 絕不會在測試中真的發網路請求

不驗運算結果或方向。
"""

import pandas as pd
import pytest

from layers.base import BaseLayer, LayerModifier
from layers.regime import RegimeLayer


# ─────────────────────────────────────────────────────
# RegimeLayer：純計算
# ─────────────────────────────────────────────────────

def test_regime_layer_runs_on_synthetic(ohlcv_df):
    layer = RegimeLayer(enabled=True)
    mod = layer.compute_modifier("TEST", ohlcv_df, sector_id="")
    assert isinstance(mod, LayerModifier)
    assert mod.layer_name == "regime"
    # 200 根足夠，應該 active
    assert mod.active is True
    # 乘數應在合理範圍
    assert 0.0 <= mod.buy_multiplier <= 3.0
    assert 0.0 <= mod.sell_multiplier <= 3.0


def test_regime_layer_disabled():
    layer = RegimeLayer(enabled=False)
    df = pd.DataFrame({"close": [100, 101, 102]})
    mod = layer.compute_modifier("TEST", df)
    assert mod.active is False


def test_regime_layer_insufficient_data(tiny_ohlcv_df):
    """資料 < 120 根時不應 crash，回傳 inactive。"""
    layer = RegimeLayer(enabled=True)
    mod = layer.compute_modifier("TEST", tiny_ohlcv_df)
    assert isinstance(mod, LayerModifier)
    assert mod.active is False


# ─────────────────────────────────────────────────────
# ChipFlowLayer：外部資料拿不到時應安全降級
# ─────────────────────────────────────────────────────

def test_chipflow_layer_graceful_when_no_data(monkeypatch, ohlcv_df):
    from layers import chipflow as cf

    monkeypatch.setattr(cf, "fetch_chip_summary", lambda symbol, days=5: None)
    layer = cf.ChipFlowLayer(enabled=True)
    mod = layer.compute_modifier("2330.TW", ohlcv_df)

    assert isinstance(mod, LayerModifier)
    assert mod.active is False
    assert mod.layer_name == "chipflow"


def test_chipflow_layer_disabled(ohlcv_df):
    from layers import chipflow as cf
    layer = cf.ChipFlowLayer(enabled=False)
    mod = layer.compute_modifier("2330.TW", ohlcv_df)
    assert mod.active is False


# ─────────────────────────────────────────────────────
# FundamentalLayer：外部資料拿不到時應安全降級
# ─────────────────────────────────────────────────────

def test_fundamental_layer_graceful_when_no_data(monkeypatch, ohlcv_df):
    from layers import fundamental as fd

    monkeypatch.setattr(fd, "fetch_twse_pe_all", lambda: {})
    layer = fd.FundamentalLayer(enabled=True)
    mod = layer.compute_modifier("2330.TW", ohlcv_df)

    assert isinstance(mod, LayerModifier)
    assert mod.active is False
    assert mod.layer_name == "fundamental"


def test_fundamental_layer_disabled(ohlcv_df):
    from layers import fundamental as fd
    layer = fd.FundamentalLayer(enabled=False)
    mod = layer.compute_modifier("2330.TW", ohlcv_df)
    assert mod.active is False


# ─────────────────────────────────────────────────────
# SentimentLayer：外部資料拿不到時應安全降級
# ─────────────────────────────────────────────────────

def test_sentiment_layer_graceful_when_no_data(monkeypatch, ohlcv_df):
    from layers import sentiment as st

    # 同時阻擋 RSS 與 fundamental 內部呼叫
    monkeypatch.setattr(st, "fetch_rss_articles", lambda: [])
    from layers import fundamental as fd
    monkeypatch.setattr(fd, "fetch_twse_pe_all", lambda: {})

    layer = st.SentimentLayer(enabled=True)
    mod = layer.compute_modifier("2330.TW", ohlcv_df)

    assert isinstance(mod, LayerModifier)
    assert mod.active is False
    assert mod.layer_name == "sentiment"


def test_sentiment_layer_disabled(ohlcv_df):
    from layers import sentiment as st
    layer = st.SentimentLayer(enabled=False)
    mod = layer.compute_modifier("2330.TW", ohlcv_df)
    assert mod.active is False


# ─────────────────────────────────────────────────────
# 確認所有 layer 都繼承 BaseLayer
# ─────────────────────────────────────────────────────

def test_all_known_layers_subclass_base():
    from layers import chipflow, fundamental, sentiment, regime
    for cls in [
        regime.RegimeLayer,
        chipflow.ChipFlowLayer,
        fundamental.FundamentalLayer,
        sentiment.SentimentLayer,
    ]:
        assert issubclass(cls, BaseLayer), f"{cls.__name__} 必須繼承 BaseLayer"
