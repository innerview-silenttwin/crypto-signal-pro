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


def test_crypto_flow_without_data_files_no_crash(tmp_path):
    """data_dir 指到空目錄時，layer 不應該 crash，應回安全 modifier。"""
    from layers.crypto_flow import CryptoFlowLayer
    layer = CryptoFlowLayer(enabled=True, data_dir=str(tmp_path))
    mod = layer.compute_modifier("BTC/USDT", _make_crypto_df())
    assert isinstance(mod, LayerModifier)
    assert mod.layer_name == "crypto_flow"
    assert 0 <= mod.buy_multiplier <= 3.0
    assert 0 <= mod.sell_multiplier <= 3.0


# ── get_flow_snapshot / _classify_fng（A2：endpoint 邏輯抽進 layer）──

@pytest.mark.parametrize("fng,expected", [
    (0, "極度恐懼"), (25, "極度恐懼"),       # 邊界：<=25
    (26, "恐懼"), (45, "恐懼"),              # 邊界：<=45
    (46, "中性"), (50, "中性"), (55, "中性"),  # 邊界：<=55
    (56, "貪婪"), (75, "貪婪"),              # 邊界：<=75
    (76, "極度貪婪"), (100, "極度貪婪"),       # > 75
])
def test_classify_fng_boundaries(fng, expected):
    """標準 Fear&Greed 顯示分級門檻（25/45/55/75），與 endpoint 原邏輯逐字一致。"""
    from layers.crypto_flow import CryptoFlowLayer
    assert CryptoFlowLayer._classify_fng(fng) == expected


def test_get_flow_snapshot_schema_no_data(tmp_path):
    """無資料時 snapshot 仍回完整 schema、不 crash（fng/fr_pct 退中性值 → 中性）。"""
    from layers.crypto_flow import CryptoFlowLayer
    snap = CryptoFlowLayer(enabled=True, data_dir=str(tmp_path)).get_flow_snapshot()
    assert set(snap) == {"fear_greed", "fng_class", "funding_rate_pct"}
    assert snap["fear_greed"] == 50.0
    assert snap["fng_class"] == "中性"
    assert snap["funding_rate_pct"] == 50.0
