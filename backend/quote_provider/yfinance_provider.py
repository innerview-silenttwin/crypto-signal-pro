"""YFinanceProvider — 包既有 yf.Ticker.history 行為，作為預設 / fallback。"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# yfinance period 合法值（_PERIOD_VALUES）
_YF_VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}


class YFinanceProvider:
    name = "yfinance"

    def get_history(
        self,
        symbol: str,
        period_days: int = 5,
        interval: str = "1d",
    ) -> Optional[pd.DataFrame]:
        try:
            ticker = yf.Ticker(symbol)
            if interval == "1m":
                # 1m 一次最多回約 7 天，period_days 此處被忽略
                df = ticker.history(period="1d", interval="1m", auto_adjust=False)
            elif period_days <= 5:
                # 合法 period 直接用
                df = ticker.history(period=f"{period_days}d", interval=interval, auto_adjust=False)
            else:
                # period_days > 5 不是合法 yfinance period（"365d" 行為不可預期）
                # → 改用 start/end；多加 7 天緩衝給週末假日
                end = datetime.now()
                start = end - timedelta(days=period_days + 7)
                df = ticker.history(start=start, end=end, interval=interval, auto_adjust=False)
            if df is None or df.empty:
                return None
            df.columns = [c.lower() for c in df.columns]
            keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
            df = df[keep].dropna(subset=["close"])
            return df if not df.empty else None
        except Exception as e:
            logger.warning("yfinance get_history(%s) failed: %s", symbol, e)
            return None
