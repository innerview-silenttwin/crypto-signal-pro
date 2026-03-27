"""
分析層 (Analysis Layers)

在技術指標基礎分數之上，疊加多層修正器：
- RegimeLayer: 盤勢辨識（趨勢/盤整/反轉）
- FundamentalLayer: 基本面 P/E 分析（Phase 2）
- SentimentLayer: 消息面情緒分析（Phase 3）
"""

from .base import BaseLayer, LayerModifier, LayerRegistry
from .regime import RegimeLayer
from .fundamental import FundamentalLayer
from .sentiment import SentimentLayer
