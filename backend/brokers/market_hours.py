"""統一台股交易時段判斷（取代 main.py 與 sector_auto_trader 的三套不同邏輯）。

時段定義：
- is_signal_window:  09:00–13:30  → 用即時 1m 行情 + 觸發訊號
- is_orderable_now:  09:00–13:25  → 可下整股 ROD/LMT 單（13:25 後不再下新單，避免收盤前不成交）
- is_market_open:    09:00–13:30  → 「市場開盤中」泛用判斷（會被 main.py 的 1D K bar 完整性邏輯使用）

節假日：
- 週六日永遠關
- 額外節假日由 data/tw_holidays.yaml 提供（YYYY-MM-DD 字串），找不到檔案就只擋週末

除權息凍結（避免假停損）：
- data/ex_dividend_calendar.yaml: {symbol: [date_str, ...]}
- 除權息日 D 與 D+1（兩天）內，is_within_ex_div_freeze() 為 True
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

_TW_TZ = pytz.timezone("Asia/Taipei")

# 整股交易時段（單位：HH:MM 字串供 config override）
SIGNAL_START = (9, 0)
SIGNAL_END = (13, 30)
ORDER_END_MINUTES_BEFORE_CLOSE = 5   # 13:25 後停止下新單

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "data")
_HOLIDAYS_FILE = os.path.normpath(os.path.join(_DATA_DIR, "tw_holidays.yaml"))
_EX_DIV_FILE = os.path.normpath(os.path.join(_DATA_DIR, "ex_dividend_calendar.yaml"))


def now_tw() -> datetime:
    return datetime.now(_TW_TZ)


def _to_minutes(hh_mm: tuple[int, int]) -> int:
    return hh_mm[0] * 60 + hh_mm[1]


def _load_yaml(path: str) -> dict:
    """簡易 YAML 載入；缺檔或無 PyYAML 時回 {}。"""
    if not os.path.exists(path):
        return {}
    try:
        import yaml  # 延遲 import，缺套件時退化而非崩潰
    except ImportError:
        logger.warning("PyYAML not installed; %s ignored. Install with `pip install pyyaml`.", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning("%s root must be a mapping; ignoring", path)
            return {}
        return data
    except Exception as e:
        logger.warning("Failed to load %s: %s", path, e)
        return {}


@lru_cache(maxsize=1)
def _holidays() -> frozenset[str]:
    """
    YAML schema:
        holidays:
          - 2026-01-01
          - 2026-02-08
    """
    raw = _load_yaml(_HOLIDAYS_FILE)
    items = raw.get("holidays") or []
    out = set()
    for x in items:
        if isinstance(x, str) and len(x) == 10:
            out.add(x)
        elif isinstance(x, date):
            out.add(x.strftime("%Y-%m-%d"))
    return frozenset(out)


@lru_cache(maxsize=1)
def _ex_div_calendar() -> dict[str, frozenset[str]]:
    """
    YAML schema:
        "2330.TW":
          - 2026-06-19
        "2454.TW":
          - 2026-07-15
    """
    raw = _load_yaml(_EX_DIV_FILE)
    out: dict[str, set[str]] = {}
    for sym, dates in raw.items():
        if not isinstance(dates, list):
            continue
        normed = set()
        for x in dates:
            if isinstance(x, str) and len(x) == 10:
                normed.add(x)
            elif isinstance(x, date):
                normed.add(x.strftime("%Y-%m-%d"))
        if normed:
            out[str(sym)] = normed
    return {k: frozenset(v) for k, v in out.items()}


def reload_calendars() -> None:
    """測試或長期執行時手動 invalidate cache。"""
    _holidays.cache_clear()
    _ex_div_calendar.cache_clear()


# ── 對外 API ──

def is_weekend(now: Optional[datetime] = None) -> bool:
    n = now or now_tw()
    return n.weekday() >= 5


def is_holiday(today_str: Optional[str] = None) -> bool:
    s = today_str or now_tw().strftime("%Y-%m-%d")
    return s in _holidays()


def is_signal_window(now: Optional[datetime] = None) -> bool:
    """09:00–13:30 週一至週五，且非節假日。用於『要不要算信號 / 抓即時價』。"""
    n = now or now_tw()
    if is_weekend(n) or is_holiday(n.strftime("%Y-%m-%d")):
        return False
    cur = n.hour * 60 + n.minute
    return _to_minutes(SIGNAL_START) <= cur <= _to_minutes(SIGNAL_END)


def is_orderable_now(now: Optional[datetime] = None) -> bool:
    """09:00–13:25 週一至週五，非節假日。用於『現在可不可以下新單』。"""
    n = now or now_tw()
    if is_weekend(n) or is_holiday(n.strftime("%Y-%m-%d")):
        return False
    cur = n.hour * 60 + n.minute
    end = _to_minutes(SIGNAL_END) - ORDER_END_MINUTES_BEFORE_CLOSE
    return _to_minutes(SIGNAL_START) <= cur <= end


def is_market_open(now: Optional[datetime] = None) -> bool:
    """泛用『市場開盤中』判斷。等同 is_signal_window，提供別名給 main.py 沿用。"""
    return is_signal_window(now)


# 收盤後給 1m K bar / 行情 cache 的緩衝（14:15 = 收盤後 45 分）
DATA_CAPTURE_END = (14, 15)


def is_data_capture_window(now: Optional[datetime] = None) -> bool:
    """給 main.py 抓 1m 行情用：09:00 至 14:15（含收盤後緩衝），且為平日非節假日。

    與 is_signal_window 不同處：保留 14:15 緩衝以容許 yfinance 1m K 延遲。
    與既有 main.py is_tw_market_open 行為相容，但多了節假日感知。
    """
    n = now or now_tw()
    if is_weekend(n) or is_holiday(n.strftime("%Y-%m-%d")):
        return False
    cur = n.hour * 60 + n.minute
    return _to_minutes(SIGNAL_START) <= cur <= _to_minutes(DATA_CAPTURE_END)


def is_within_ex_div_freeze(symbol: str, today_str: Optional[str] = None) -> bool:
    """除權息日 D 與 D+1 之內 → True（呼叫端對 SELL 自動停損凍結）。

    *只*影響自動停損；BUY 與「人為觸發 SELL」不受限。
    """
    today = today_str or now_tw().strftime("%Y-%m-%d")
    cal = _ex_div_calendar()
    sym_dates = cal.get(symbol)
    if not sym_dates:
        return False
    today_d = datetime.strptime(today, "%Y-%m-%d").date()
    for d_str in sym_dates:
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d <= today_d <= d + timedelta(days=1):
            return True
    return False
