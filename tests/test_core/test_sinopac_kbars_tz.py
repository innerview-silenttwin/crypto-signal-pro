"""sinopac_provider._kbars_to_df 時區回歸測試（不打網路、不建 Shioaji session）。

Shioaji kbars.ts 是「台北牆鐘時間」編成的 ns epoch（非 UTC）。曾誤當 UTC 再 +8
轉台北 → 09:01 K 棒變 17:01（處置雷達走勢圖 X 軸實證抓到的 bug）。
"""

import os
import sys

import pandas as pd

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from quote_provider.sinopac_provider import SinopacQuoteProvider  # noqa: E402


class _FakeKbars:
    """模擬 Shioaji kbars：ts=台北牆鐘 ns epoch。"""

    def __init__(self, wall_times, closes):
        # 台北 09:01 的牆鐘 → Shioaji 給的是 naive ns epoch（數值上等於把 09:01 當 epoch 解讀）
        self.ts = [int(pd.Timestamp(t).value) for t in wall_times]
        self.Open = closes
        self.High = [c + 1 for c in closes]
        self.Low = [c - 1 for c in closes]
        self.Close = closes
        self.Volume = [1000 for _ in closes]


def test_kbars_ts_is_taipei_wall_clock_not_utc():
    """09:01 台北牆鐘的 ts 必須解析回 09:01+08:00，不可再 +8 變 17:01。"""
    kb = _FakeKbars(["2026-07-13 09:01:00", "2026-07-13 09:02:00"], [100.0, 101.0])
    df = SinopacQuoteProvider._kbars_to_df(kb)
    assert df is not None and len(df) == 2
    assert str(df.index.tz) == "Asia/Taipei"
    assert df.index[0].strftime("%H:%M") == "09:01"      # bug 時會是 17:01
    assert df.index[1].strftime("%H:%M") == "09:02"
    assert df["close"].iloc[0] == 100.0


def test_kbars_daily_resample_same_day():
    """修正後日線 resample 日界不變（09:00-13:30 都在同一天）。"""
    kb = _FakeKbars(["2026-07-13 09:01:00", "2026-07-13 13:24:00"], [100.0, 105.0])
    df = SinopacQuoteProvider._kbars_to_df(kb)
    daily = SinopacQuoteProvider._resample_to_daily(df)
    assert len(daily) == 1
    assert daily.index[0].strftime("%Y-%m-%d") == "2026-07-13"
    assert daily["close"].iloc[0] == 105.0
