"""
指標插件自動註冊器

使用裝飾器模式自動註冊所有指標插件，
讓新增指標時只需要繼承 BaseIndicator 並加上 @register_indicator 即可。
"""

from typing import Dict, Type, List, Optional
from .base import BaseIndicator


class IndicatorRegistry:
    """指標插件註冊表"""
    
    _indicators: Dict[str, Type[BaseIndicator]] = {}

    @classmethod
    def register(cls, name: str):
        """裝飾器：註冊指標插件"""
        def decorator(indicator_cls: Type[BaseIndicator]):
            cls._indicators[name] = indicator_cls
            return indicator_cls
        return decorator

    @classmethod
    def get(cls, name: str) -> Optional[Type[BaseIndicator]]:
        """取得指定名稱的指標類別"""
        return cls._indicators.get(name)

    @classmethod
    def get_all(cls) -> Dict[str, Type[BaseIndicator]]:
        """取得所有已註冊的指標"""
        return cls._indicators.copy()

    @classmethod
    def create_all(cls, weights: Optional[Dict[str, float]] = None) -> List[BaseIndicator]:
        """
        建立所有已註冊指標的實例
        
        Args:
            weights: 指標權重字典，e.g. {'rsi': 15.0, 'macd': 20.0}
        """
        instances = []
        for name, indicator_cls in cls._indicators.items():
            max_score = weights.get(name, 15.0) if weights else 15.0
            instances.append(indicator_cls(max_score=max_score))
        return instances

    @classmethod
    def list_names(cls) -> List[str]:
        """列出所有已註冊的指標名稱"""
        return list(cls._indicators.keys())


# 方便使用的裝飾器別名
register_indicator = IndicatorRegistry.register

# ============================================================
# 回測系統用 Helper Functions
# ============================================================

# 已註冊指標的 registry key → 顯示名稱對照
INDICATOR_DISPLAY = {
    "rsi":              "RSI 相對強弱",
    "macd":             "MACD 趨勢動能",
    "bollinger":        "布林通道",
    "mfi":              "MFI 資金流量",
    "ema_cross":        "EMA 均線交叉",
    "volume":           "成交量分析",
    "adx":              "ADX 趨勢強度",
    "stoch_rsi":        "隨機RSI",
    "volume_reversal":  "爆量反轉",
    "pullback_support": "均線拉回支撐",
}

NEW_INDICATOR_DISPLAY = {
    "bias":             "乖離率 (BIAS)",
    "kd":               "KD 隨機指標",
    "williams_r":       "威廉指標 %R",
}

# 確保所有指標模組被 import（觸發 @register_indicator 裝飾器）
_MODULES = [
    'rsi', 'macd', 'bollinger', 'mfi', 'ema', 'volume',
    'adx', 'stoch_rsi', 'volume_reversal', 'pullback_support',
    'bias', 'kd', 'williams_r',
]


def _ensure_imported():
    """動態 import 所有指標模組，觸發 @register_indicator"""
    import importlib
    for mod_name in _MODULES:
        try:
            importlib.import_module(f'.{mod_name}', package='indicators')
        except ImportError:
            pass


def create_all_indicators(include_new: bool = True) -> List[BaseIndicator]:
    """
    建立所有指標實例（回測引擎用）

    Args:
        include_new: 是否包含新候選指標（bias, kd, williams_r）
    Returns:
        List[BaseIndicator]: 所有指標實例
    """
    _ensure_imported()
    all_keys = set(INDICATOR_DISPLAY.keys())
    if include_new:
        all_keys |= set(NEW_INDICATOR_DISPLAY.keys())

    instances = []
    for key in all_keys:
        cls = IndicatorRegistry.get(key)
        if cls is not None:
            instances.append(cls())
    return instances


def get_indicator_keys_map(indicators: List[BaseIndicator]) -> Dict[str, str]:
    """
    建立 indicator instance → registry key 的對照表

    回傳 {indicator.name: registry_key}
    回測引擎需要用 registry key 作為信號欄位名稱
    """
    _ensure_imported()
    # 建立 name → key 反查表
    name_to_key = {}
    for key, cls in IndicatorRegistry.get_all().items():
        inst = cls()
        name_to_key[inst.name] = key

    result = {}
    for ind in indicators:
        key = name_to_key.get(ind.name)
        if key:
            result[ind.name] = key
    return result
