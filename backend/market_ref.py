"""除權息參考價查詢（共用模組）。

除權息生效日的「昨收」是除息前價（偏高），會讓以昨收為基準的計算失真：
- sector_auto_trader 的 ±10% 價格合理性防護（3034 實例：昨收 542、參考價 519、
  實價 467.5 對 542 為 -13.7% 被誤判異常，對 519 僅 -9.9% 合法）
- 處置雷達走勢圖的平盤價/漲跌幅（除息日應以參考價為平盤）

資料源 FinMind TaiwanStockDividendResult（免費）。per-code 當日快取；
抓取失敗不快取（下次重試、不 silent stale）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

_FINMIND = "https://api.finmindtrade.com/api/v4/data"
_div_ref_cache: Dict[str, dict] = {}


def get_ex_dividend_ref(symbol: str, prev_date: str, cur_date: str) -> Optional[float]:
    """若最新交易日相對前一交易日之間發生除權息（含遇假日順延），回除息參考價(after_price)。

    否則回 None。日期皆 'YYYY-MM-DD'；除息生效日落在 (prev_date, cur_date] 才算。
    """
    code = symbol.replace(".TWO", "").replace(".TW", "").strip()
    if not code.isdigit():
        return None
    c = _div_ref_cache.get(code)
    if not c or c["day"] != cur_date:
        try:
            start = (datetime.strptime(cur_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
            j = requests.get(_FINMIND, params={"dataset": "TaiwanStockDividendResult",
                                               "data_id": code, "start_date": start}, timeout=8).json()
        except Exception as e:
            logger.debug("除息資料取得失敗 %s: %s", code, e)
            return None                                    # 失敗不快取、下次重試（勿 silent stale）
        if j.get("msg") != "success":
            return None
        c = _div_ref_cache[code] = {"day": cur_date, "records": j.get("data") or []}
    for rec in c["records"]:
        exd = rec.get("date")
        if exd and prev_date < exd <= cur_date:           # 除息生效落在(前一交易日, 最新交易日]
            try:
                ref = float(rec.get("after_price") or 0)
                if ref > 0:
                    return ref
            except (TypeError, ValueError):
                continue
    return None
