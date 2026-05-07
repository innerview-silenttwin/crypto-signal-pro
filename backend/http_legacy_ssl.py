"""
Workaround：Python 3.14 + OpenSSL 3.x 預設啟用 X509_V_FLAG_X509_STRICT，
要求 intermediate CA cert 必須帶 Subject Key Identifier (SKI) 擴充欄位。

很多台灣 SSL 端點（TWSE、CMoney 等用 TWCA / Taiwan-CA 簽出的 cert chain），
intermediate 都沒有 SKI 欄位 → 升級到 Python 3.14 之後就全部踩雷，
噴 `Missing Subject Key Identifier`。

這個 module 提供一組 requests Session/get/post，**只關掉 STRICT 旗標**，
其他驗證（hostname、過期、trust chain）照樣執行。只用在已知踩雷的台灣
endpoint，不要全域 monkey-patch。

Python ≤ 3.13 行為不變（VERIFY_X509_STRICT 旗標雖然存在但預設沒開）。
"""

from __future__ import annotations
import ssl
import threading
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context


class _LegacyTaiwanSSLAdapter(HTTPAdapter):
    """HTTPAdapter 把 VERIFY_X509_STRICT 從 SSL context 拿掉。"""

    def _build_ctx(self):
        ctx = create_urllib3_context()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_ctx()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_ctx()
        return super().proxy_manager_for(*args, **kwargs)


_lock = threading.Lock()
_session: Optional[requests.Session] = None


def legacy_session() -> requests.Session:
    """Lazy singleton：一個 Session 共用 connection pool。Thread-safe。"""
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                s = requests.Session()
                adapter = _LegacyTaiwanSSLAdapter()
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _session = s
    return _session


def legacy_get(url: str, **kwargs):
    return legacy_session().get(url, **kwargs)


def legacy_post(url: str, **kwargs):
    return legacy_session().post(url, **kwargs)
