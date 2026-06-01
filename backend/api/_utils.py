"""router 內共用的工具函式（純函式，零 FastAPI / main 依賴）。

放這裡的東西必須：
- 沒有 import main 或任何 backend/api/* 模組（避免 circular import）
- 純函式 + 純標準庫或常用第三方（numpy/pandas 等）
- 多個 router 會用到
"""

import numpy as np


def sanitize_json(obj):
    """將 numpy 類型轉為 Python 原生類型，避免 FastAPI JSON 序列化錯誤。

    過去散落兩份副本（main.py + api/sector_trading.py），A6b 統一抽到此處。
    保留底線 prefix 別名 `_sanitize` 供既有 caller 平滑遷移。
    """
    if isinstance(obj, dict):
        return {k: sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_json(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


# 別名：與兩份舊副本同名，方便 caller 直接置換 import
_sanitize = sanitize_json
