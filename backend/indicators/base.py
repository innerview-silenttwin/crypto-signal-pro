"""
指標插件基底類別 (Base Indicator Plugin)

所有技術指標都必須繼承此類別，實現 calculate() 和 generate_signal() 方法。
這確保了插件架構的一致性和可擴展性。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional
import pandas as pd


class SignalType(Enum):
    """信號類型"""
    STRONG_BUY = "STRONG_BUY"      # 強烈買入
    BUY = "BUY"                     # 買入
    NEUTRAL = "NEUTRAL"             # 中性
    SELL = "SELL"                   # 賣出
    STRONG_SELL = "STRONG_SELL"    # 強烈賣出


@dataclass
class IndicatorSignal:
    """單一指標產出的信號"""
    indicator_name: str          # 指標名稱
    signal_type: SignalType      # 信號類型
    score: float                 # 分數 (0–100，衡量該指標的信號強度)
    value: float                 # 指標當前數值
    details: Dict[str, Any]      # 詳細資訊（如 RSI 值、MACD 柱狀圖等）
    reason: str                  # 產出信號的原因說明

    def __repr__(self):
        return (f"IndicatorSignal({self.indicator_name}: "
                f"{self.signal_type.value}, score={self.score:.1f}, "
                f"reason='{self.reason}')")


class BaseIndicator(ABC):
    """
    指標插件基底類別
    
    所有自定義指標必須繼承此類別並實現：
      - calculate(df): 在 DataFrame 上計算指標列
      - generate_signal(df): 根據計算結果產生買賣信號
    """

    def __init__(self, name: str, max_score: float = 15.0, params: Optional[Dict] = None):
        """
        Args:
            name: 指標名稱
            max_score: 此指標可貢獻的最高分數
            params: 可配置參數
        """
        self.name = name
        self.max_score = max_score
        self.params = params or {}

    @abstractmethod
    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        計算指標並將結果新增為 DataFrame 的新欄位。
        
        Args:
            df: 包含 OHLCV 資料的 DataFrame
                必須包含: open, high, low, close, volume
        Returns:
            df: 新增了指標欄位的 DataFrame
        """
        pass

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        """
        根據最新一行的指標數值產生買賣信號。
        
        Args:
            df: 已經過 calculate() 處理的 DataFrame
        Returns:
            IndicatorSignal: 包含信號類型、分數和原因
        """
        pass

    def _scale_score(self, raw_score: float) -> float:
        """將原始分數縮放到 [0, max_score] 範圍"""
        return max(0.0, min(self.max_score, raw_score))

    def get_params(self) -> Dict:
        """取得指標參數"""
        return self.params.copy()

    def set_params(self, params: Dict):
        """更新指標參數"""
        self.params.update(params)

    def __repr__(self):
        return f"{self.__class__.__name__}(name='{self.name}', max_score={self.max_score})"
