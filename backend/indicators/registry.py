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
