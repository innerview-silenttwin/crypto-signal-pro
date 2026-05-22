"""SinopacQuoteProvider — 永豐報價（quote-only，無 CA，絕不下單）。

設計關鍵：
- `Shioaji(simulation=False)` 連 production 主機拿真實報價
- **故意不呼叫 `activate_ca()`** → 物理上無法 place_order，是 defense-in-depth
- 只暴露 `get_history()` → 內部僅用 `api.kbars()`
- 對外不暴露 self._api：用雙底線 name mangling（__api）讓誤觸成本更高
"""
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import pytz

logger = logging.getLogger(__name__)

_TW_TZ = pytz.timezone("Asia/Taipei")
# Shioaji kbars 預設回 1m K（contract 文件）
_KBARS_NATIVE_INTERVAL = "1m"


class SinopacQuoteProvider:
    name = "sinopac"

    def __init__(
        self,
        *,
        simulation: bool = False,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        person_id: Optional[str] = None,
    ):
        api_key = api_key or os.environ.get("SHIOAJI_API_KEY")
        secret_key = secret_key or os.environ.get("SHIOAJI_SECRET_KEY")
        person_id = person_id or os.environ.get("SHIOAJI_PERSON_ID")
        if not (api_key and secret_key):
            raise RuntimeError("SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY not set")

        try:
            import shioaji as sj
        except ImportError as e:
            raise RuntimeError("shioaji package not installed") from e

        self._simulation = simulation
        self._lock = threading.RLock()
        # shioaji 1.5.x 沒有 logger kwarg，改用 stdlib 把它的 logger 拉到 WARNING 避免列印 credentials
        logging.getLogger("shioaji").setLevel(logging.WARNING)
        # 雙底線 name mangling：外部要寫 _SinopacQuoteProvider__api 才拿得到，意外觸發機率歸零
        self.__api = sj.Shioaji(simulation=simulation)
        self.__api.login(api_key=api_key, secret_key=secret_key)
        # 明確不呼叫 self.__api.activate_ca()  ← 安全核心
        logger.info(
            "SinopacQuoteProvider ready (simulation=%s, CA=disabled)",
            simulation,
        )

    def get_history(
        self,
        symbol: str,
        period_days: int = 5,
        interval: str = "1d",
    ) -> Optional[pd.DataFrame]:
        # 指數 / 非個股代號（^TWII 等）永豐 Stocks contract 沒有，直接 fallback 到 yfinance
        if symbol.startswith("^") or symbol.startswith("00"):
            # 註：00 開頭暫不強制 fallback（部份 ETF 永豐有），先只處理 ^ 前綴
            if symbol.startswith("^"):
                return self._yfinance_fallback(symbol, period_days, interval)

        contract = self._resolve_contract(symbol)
        if contract is None:
            logger.warning("contract not found: %s; fallback yfinance", symbol)
            return self._yfinance_fallback(symbol, period_days, interval)

        end = datetime.now(_TW_TZ).date()
        # 1m 只要近 2 個交易日就夠（盤中拿最後一筆）；1d 用 period_days
        days = 2 if interval == "1m" else max(period_days, 5)
        start = end - timedelta(days=days + 7)  # +7 緩衝給週末假日

        try:
            with self._lock:
                kbars = self.__api.kbars(
                    contract=contract,
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                )
        except Exception as e:
            logger.warning("sinopac kbars(%s) failed: %s", symbol, e.__class__.__name__)
            return None

        df = self._kbars_to_df(kbars)
        if df is None or df.empty:
            return None

        # Shioaji kbars 永遠是 1m → 1d 時必須 resample
        if interval == "1d":
            df = self._resample_to_daily(df)
            if len(df) > period_days:
                df = df.tail(period_days)

        return df if not df.empty else None

    # ── internals ─────────────────────────────────────────────────

    @staticmethod
    def _yfinance_fallback(symbol: str, period_days: int, interval: str):
        """永豐沒有的標的（指數、特殊代號）→ 用 yfinance 補。"""
        try:
            from .yfinance_provider import YFinanceProvider
            return YFinanceProvider().get_history(symbol, period_days=period_days, interval=interval)
        except Exception as e:
            logger.warning("yfinance fallback for %s failed: %s", symbol, e)
            return None

    def _resolve_contract(self, symbol: str):
        """symbol 例 '2330.TW' → Contracts.Stocks['2330']."""
        code = symbol.split(".")[0]
        try:
            with self._lock:
                return self.__api.Contracts.Stocks[code]
        except (KeyError, AttributeError):
            try:
                with self._lock:
                    return getattr(self.__api.Contracts.Stocks, code)
            except Exception:
                return None

    @staticmethod
    def _kbars_to_df(kbars) -> Optional[pd.DataFrame]:
        """Shioaji kbars 物件 → 標準 DataFrame。

        - columns: open/high/low/close/volume（小寫）
        - index: DatetimeIndex with Asia/Taipei tz（Shioaji ts 是 UTC ns epoch）
        """
        try:
            data = {
                "open": list(kbars.Open),
                "high": list(kbars.High),
                "low": list(kbars.Low),
                "close": list(kbars.Close),
                "volume": list(kbars.Volume),
            }
            ts = list(kbars.ts)
            # UTC ns epoch → tz-aware Asia/Taipei，避免下游 .astimezone 對 naive 丟 TypeError
            idx = pd.to_datetime(ts, unit="ns", utc=True).tz_convert(_TW_TZ)
            df = pd.DataFrame(data, index=idx)
            df = df.dropna(subset=["close"])
            return df
        except Exception as e:
            logger.warning("kbars_to_df failed: %s", e)
            return None

    @staticmethod
    def _resample_to_daily(df: pd.DataFrame) -> pd.DataFrame:
        """1m kbars → 日線（在 Asia/Taipei tz 下切日界）."""
        if df.empty:
            return df
        # df.index 必為 tz-aware Asia/Taipei（_kbars_to_df 保證），resample 自動用該時區的午夜
        daily = df.resample("1D").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["close"])
        daily = daily[daily["volume"] > 0]
        return daily
