"""
分析層基礎類別

每個 Layer 是一個「分數修正器」，不取代原有 0-100 分系統，
而是根據額外資訊調整最終買賣分數。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import pandas as pd


@dataclass
class LayerModifier:
    """層修正結果"""
    layer_name: str
    active: bool = True

    # 分數修正
    buy_multiplier: float = 1.0    # 買入分數乘數 (0.0~2.0)
    sell_multiplier: float = 1.0   # 賣出分數乘數 (0.0~2.0)
    buy_offset: float = 0.0        # 買入分數偏移 (-30~+30)
    sell_offset: float = 0.0       # 賣出分數偏移 (-30~+30)

    # 特殊控制
    veto_buy: bool = False         # 否決買入（如反轉期）
    veto_sell: bool = False        # 否決賣出（如強勢起漲）

    # 說明
    regime: str = ""               # 當前盤勢狀態
    reason: str = ""               # 人類可讀原因
    details: Dict = field(default_factory=dict)  # 詳細數據


class BaseLayer(ABC):
    """分析層抽象基礎類別"""

    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled

    @abstractmethod
    def compute_modifier(self, symbol: str, df: pd.DataFrame,
                         sector_id: str = "") -> LayerModifier:
        """
        計算修正值

        Args:
            symbol: 股票代碼
            df: 含技術指標的 DataFrame
            sector_id: 類股 ID

        Returns:
            LayerModifier 修正結果
        """
        pass


class LayerRegistry:
    """層註冊表，管理所有可用的分析層"""

    _layers: Dict[str, type] = {}

    @classmethod
    def register(cls, name: str, layer_class: type):
        cls._layers[name] = layer_class

    @classmethod
    def create(cls, name: str, **kwargs) -> Optional[BaseLayer]:
        if name in cls._layers:
            return cls._layers[name](**kwargs)
        return None

    @classmethod
    def create_all(cls, config: Dict = None) -> List[BaseLayer]:
        """根據配置建立所有啟用的層"""
        config = config or {}
        layers = []
        for name, layer_cls in cls._layers.items():
            layer_config = config.get(name, {})
            if layer_config.get("enabled", True):
                layers.append(layer_cls(**layer_config))
        return layers
