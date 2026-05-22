"""QuoteProvider Protocol — 統一介面，讓上層不需知道資料是 yfinance 還是永豐。

回傳 DataFrame 規格（與既有 sector_auto_trader 期望一致）：
- columns: open / high / low / close / volume（小寫）
- index: DatetimeIndex（pandas Timestamp，含時區資訊或 naive 皆可）
- 若資料不足 / 抓不到 → return None（不丟例外）
"""
from typing import Optional, Protocol

import pandas as pd


class QuoteProvider(Protocol):
    name: str

    def get_history(
        self,
        symbol: str,
        period_days: int = 5,
        interval: str = "1d",
    ) -> Optional[pd.DataFrame]:
        """取近 period_days 天的 K 線。

        interval: "1d" / "1m"（其它間隔依實作支援度）
        symbol: 含後綴的台股代號（"2330.TW" / "0050.TWO"）；
                provider 內部自行轉成 native 格式。
        """
        ...
