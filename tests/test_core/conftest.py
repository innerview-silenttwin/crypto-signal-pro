"""讓 tests/test_core/* 能 import backend 內部模組。"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

_BACKEND_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "backend")
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


@pytest.fixture
def ohlcv_df():
    """合成 200 根日 K 線的 OHLCV DataFrame，用於 smoke test。

    特意產生既有上升也有下降的價量資料，讓多數指標能正常計算
    （不需要外部資料、不需要網路 IO）。
    """
    rng = np.random.default_rng(seed=42)
    n = 200
    base = 100.0
    # 用 random walk + 緩慢趨勢產生 close
    drift = np.linspace(0, 30, n)
    noise = rng.normal(0, 1.5, n).cumsum()
    close = base + drift + noise
    close = np.maximum(close, 1.0)  # 避免價格負

    high = close + rng.uniform(0.5, 2.0, n)
    low = close - rng.uniform(0.5, 2.0, n)
    low = np.maximum(low, 0.5)
    open_ = close + rng.uniform(-1.0, 1.0, n)
    volume = rng.uniform(1_000_000, 5_000_000, n)

    idx = pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="D")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


@pytest.fixture
def tiny_ohlcv_df():
    """資料極少（10 根）的 DataFrame，驗證『資料不足』分支不會 crash。"""
    rng = np.random.default_rng(seed=1)
    n = 10
    close = 100 + rng.normal(0, 1, n).cumsum()
    df = pd.DataFrame(
        {
            "open": close + rng.uniform(-0.5, 0.5, n),
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": rng.uniform(1_000_000, 2_000_000, n),
        },
        index=pd.date_range(end=pd.Timestamp.now().normalize(), periods=n, freq="D"),
    )
    return df
