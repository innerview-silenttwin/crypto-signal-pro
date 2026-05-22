"""Factory：從 env 選 quote provider。預設 yfinance，不破壞既有行為。

Sinopac init 失敗時 fallback 到 yfinance，但**不永久 cache** —— 5 分鐘 cooldown 後
下次呼叫會再試一次，避免「開機抖一下整天都用 yfinance」。
"""
import logging
import os
import threading
import time
from typing import Optional

from .base import QuoteProvider
from .yfinance_provider import YFinanceProvider

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_instance: Optional[QuoteProvider] = None
_last_sinopac_fail_ts: float = 0.0
_SINOPAC_RETRY_COOLDOWN_S = 5 * 60


def get_quote_provider() -> QuoteProvider:
    """Singleton。env QUOTE_SOURCE=yfinance|sinopac，預設 yfinance。

    若 source=sinopac 但 init 失敗 → 暫時 fallback 到 yfinance（不 cache）；
    5 分鐘後下次呼叫會再試一次。
    """
    global _instance, _last_sinopac_fail_ts

    source = (os.environ.get("QUOTE_SOURCE") or "yfinance").strip().lower()

    # yfinance：穩定 singleton
    if source != "sinopac":
        if _instance is not None and _instance.name == "yfinance":
            return _instance
        with _lock:
            if _instance is None or _instance.name != "yfinance":
                _instance = YFinanceProvider()
                logger.info("Quote provider: yfinance")
            return _instance

    # sinopac：成功則 cache；失敗則暫時走 yfinance、cooldown 後重試
    with _lock:
        if _instance is not None and _instance.name == "sinopac":
            return _instance

        now = time.time()
        if now - _last_sinopac_fail_ts < _SINOPAC_RETRY_COOLDOWN_S:
            # cooldown 內：用 yfinance 但不 cache，等 cooldown 結束再試
            return YFinanceProvider()

        try:
            from .sinopac_provider import SinopacQuoteProvider
            _instance = SinopacQuoteProvider(simulation=False)
            logger.info("Quote provider: sinopac (production, no CA)")
            return _instance
        except Exception as e:
            _last_sinopac_fail_ts = now
            logger.error(
                "SinopacQuoteProvider init failed (%s); fallback yfinance for %ds",
                e, _SINOPAC_RETRY_COOLDOWN_S,
            )
            return YFinanceProvider()


def reset_quote_provider() -> None:
    """測試用：清掉 singleton 與 cooldown 狀態。"""
    global _instance, _last_sinopac_fail_ts
    with _lock:
        _instance = None
        _last_sinopac_fail_ts = 0.0
