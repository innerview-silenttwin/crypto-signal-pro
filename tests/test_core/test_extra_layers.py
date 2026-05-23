"""Smoke test：active_etf + crypto_flow 模組。

active_etf 是 module-level helper（非 BaseLayer 子類），驗工具函式 graceful 行為。
crypto_flow 是 BaseLayer 子類，驗 enabled/disabled + 無資料時不 crash。
"""

import pandas as pd
import pytest

from layers.base import BaseLayer, LayerModifier


# ─────────────────────────────────────────────────────
# active_etf
# ─────────────────────────────────────────────────────

def test_active_etf_get_score_returns_none_when_no_cache(monkeypatch):
    """快取不存在/未填時，get_active_etf_score 應該回 None 而非 crash。"""
    from layers import active_etf
    monkeypatch.setattr(active_etf, "_scores_cache", {})
    monkeypatch.setattr(active_etf, "_ensure_cache", lambda: None)
    result = active_etf.get_active_etf_score("0050.TW")
    # 沒資料時回 None 或 0，兩種都可接受——只要不 crash
    assert result is None or result == 0


def test_active_etf_get_holders_returns_list_or_empty(monkeypatch):
    from layers import active_etf
    monkeypatch.setattr(active_etf, "_etf_holders_cache", {})
    monkeypatch.setattr(active_etf, "_ensure_cache", lambda: None)
    result = active_etf.get_active_etf_holders("0050.TW")
    assert isinstance(result, list)


def test_active_etf_get_ranking_returns_dict(monkeypatch):
    """get_active_etf_ranking 在無資料時應回空 dict 或合法 dict 結構。"""
    from layers import active_etf
    monkeypatch.setattr(active_etf, "_ensure_cache", lambda: None)
    result = active_etf.get_active_etf_ranking()
    assert isinstance(result, dict)


# ─────────────────────────────────────────────────────
# crypto_flow
# ─────────────────────────────────────────────────────

def _make_crypto_df(n=10):
    idx = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="D")
    return pd.DataFrame({"close": [100.0 + i for i in range(n)]}, index=idx)


def test_crypto_flow_layer_is_baselayer():
    from layers.crypto_flow import CryptoFlowLayer
    assert issubclass(CryptoFlowLayer, BaseLayer)


def test_crypto_flow_disabled():
    from layers.crypto_flow import CryptoFlowLayer
    layer = CryptoFlowLayer(enabled=False)
    mod = layer.compute_modifier("BTC/USDT", _make_crypto_df())
    assert isinstance(mod, LayerModifier)
    assert mod.active is False


@pytest.mark.xfail(
    reason="CryptoFlowLayer.__init__ 未初始化 self._fr_daily，"
    "當 btc_funding_rate.csv 不存在時 _get_funding_rate 會丟 AttributeError。"
    "此 bug 被 SignalAggregator 的 try/except 接住所以實務上沉默失效；"
    "需要使用者授權才修，先用 xfail 留下記錄。",
    strict=True,
)
def test_crypto_flow_without_data_files_no_crash(tmp_path):
    """data_dir 指到空目錄時，layer 不應該 crash，應回安全 modifier。"""
    from layers.crypto_flow import CryptoFlowLayer
    layer = CryptoFlowLayer(enabled=True, data_dir=str(tmp_path))
    mod = layer.compute_modifier("BTC/USDT", _make_crypto_df())
    assert isinstance(mod, LayerModifier)
    assert mod.layer_name == "crypto_flow"
    assert 0 <= mod.buy_multiplier <= 3.0
    assert 0 <= mod.sell_multiplier <= 3.0
