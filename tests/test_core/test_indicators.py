"""Smoke test：indicators 插件契約。

只驗 API contract（型別、欄位齊全、分數範圍合法、不 crash），
**不驗運算結果是否正確或方向是否符合預期**——核心邏輯不在這層測試的責任範圍。
"""

import pandas as pd
import pytest

from indicators.base import BaseIndicator, IndicatorSignal, SignalType
from indicators.registry import (
    IndicatorRegistry,
    INDICATOR_DISPLAY,
    NEW_INDICATOR_DISPLAY,
    create_all_indicators,
    _ensure_imported,
)


def test_registry_loads_all_known_indicators():
    """所有 INDICATOR_DISPLAY / NEW_INDICATOR_DISPLAY 列出的指標都有被註冊。"""
    _ensure_imported()
    registered = set(IndicatorRegistry.list_names())
    expected = set(INDICATOR_DISPLAY.keys()) | set(NEW_INDICATOR_DISPLAY.keys())
    missing = expected - registered
    assert not missing, f"以下指標宣告在 display 表但未註冊：{missing}"


def test_create_all_indicators_returns_instances():
    """create_all_indicators() 回傳的物件都是 BaseIndicator 子類實例。"""
    indicators = create_all_indicators(include_new=True)
    assert len(indicators) > 0
    for ind in indicators:
        assert isinstance(ind, BaseIndicator)
        assert ind.name, f"{type(ind).__name__} 缺少 name"
        assert ind.max_score > 0, f"{ind.name} max_score 必須 > 0"


@pytest.mark.parametrize("registry_key", list(INDICATOR_DISPLAY.keys()) + list(NEW_INDICATOR_DISPLAY.keys()))
def test_each_indicator_contract(ohlcv_df, registry_key):
    """每個指標：calculate() + generate_signal() 在 200 根合成資料上不 crash，
    回傳合法 IndicatorSignal。"""
    _ensure_imported()
    cls = IndicatorRegistry.get(registry_key)
    assert cls is not None, f"{registry_key} 未註冊"
    ind = cls()

    # calculate 不 crash，回傳 DataFrame
    df = ind.calculate(ohlcv_df.copy())
    assert isinstance(df, pd.DataFrame)
    assert len(df) == len(ohlcv_df), "calculate 不應改變列數"

    # generate_signal 回傳合法 IndicatorSignal
    sig = ind.generate_signal(df)
    assert isinstance(sig, IndicatorSignal)
    assert sig.indicator_name == ind.name
    assert isinstance(sig.signal_type, SignalType)
    assert 0.0 <= sig.score <= ind.max_score, (
        f"{ind.name} 分數 {sig.score} 超出 [0, {ind.max_score}]"
    )
    assert sig.reason, f"{ind.name} reason 不應為空"
    assert isinstance(sig.details, dict)


@pytest.mark.parametrize("registry_key", list(INDICATOR_DISPLAY.keys()) + list(NEW_INDICATOR_DISPLAY.keys()))
def test_each_indicator_handles_insufficient_data(tiny_ohlcv_df, registry_key):
    """資料不足時不應 crash，應回傳 NEUTRAL 或合法 IndicatorSignal。"""
    _ensure_imported()
    cls = IndicatorRegistry.get(registry_key)
    ind = cls()
    df = ind.calculate(tiny_ohlcv_df.copy())
    sig = ind.generate_signal(df)
    assert isinstance(sig, IndicatorSignal)
    assert isinstance(sig.signal_type, SignalType)
    # 資料不足分支通常 score=0，但不強制——只要在合法範圍即可
    assert 0.0 <= sig.score <= ind.max_score
