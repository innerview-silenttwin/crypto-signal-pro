"""大盤籌碼揭露（Phase 1：市場層級）。

純資料揭露、**不做任何買賣判斷**。提供「推敲大盤走勢」最相關、且免費可得的三項：
1. 台指期三大法人未平倉淨額（外資/投信/自營）— 期貨方向領先指標
2. 大盤三大法人現貨買賣超（金額，外資/投信/自營）
3. 選擇權 Put/Call 比（成交量比 + 未平倉量比）— 情緒

資料源：FinMind（匿名免費）+ TAIFEX 官方 CSV。皆用本地快取 + TTL，避免打爆 rate limit。
（官股分點需付費資料源、本期略過；個股層級借券/當沖/投信連買/大戶為 Phase 2。）
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

_FINMIND = "https://api.finmindtrade.com/api/v4/data"
_TAIFEX_PC = "https://www.taifex.com.tw/cht/3/pcRatioDown"

_CACHE_TTL = 1800  # 30 分鐘
_cache: dict = {}  # key -> (timestamp, data)


def _cached(key: str, ttl: int = _CACHE_TTL):
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _store(key: str, data):
    _cache[key] = (time.time(), data)
    return data


def _start_date(days: int) -> str:
    # 多抓一倍日曆天以涵蓋約 days 個交易日
    return (datetime.now() - timedelta(days=days * 2 + 5)).strftime("%Y-%m-%d")


def _finmind(dataset: str, days: int, data_id: str | None = None) -> list[dict]:
    params = {"dataset": dataset, "start_date": _start_date(days)}
    if data_id:
        params["data_id"] = data_id
    try:
        r = requests.get(_FINMIND, params=params, timeout=15)
        j = r.json()
        if j.get("msg") != "success":
            logger.warning("FinMind %s 非 success: %s", dataset, j.get("msg"))
            return []
        return j.get("data", []) or []
    except Exception as e:
        logger.warning("FinMind %s 抓取失敗: %s", dataset, e)
        return []


def _net(buy, sell) -> float:
    try:
        return float(buy or 0) - float(sell or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_futures_oi(days: int = 20) -> list[dict]:
    """台指期(TX)三大法人未平倉淨額（口數，淨 = 多單未平倉 - 空單未平倉）。

    回 [{date, foreign, trust, dealer}]（依日期遞增），淨口數正=偏多、負=偏空。
    """
    key = f"fut_oi:{days}"
    c = _cached(key)
    if c is not None:
        return c
    rows = _finmind("TaiwanFuturesInstitutionalInvestors", days, data_id="TX")
    by_date: dict[str, dict] = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        inv = r.get("institutional_investors")
        net = _net(r.get("long_open_interest_balance_volume"),
                   r.get("short_open_interest_balance_volume"))
        slot = by_date.setdefault(d, {"date": d, "foreign": 0.0, "trust": 0.0, "dealer": 0.0})
        if inv == "外資":
            slot["foreign"] += net
        elif inv == "投信":
            slot["trust"] += net
        elif inv == "自營商":
            slot["dealer"] += net
    out = [by_date[d] for d in sorted(by_date)][-days:]
    return _store(key, out)


def fetch_market_institutional(days: int = 20) -> list[dict]:
    """大盤三大法人現貨買賣超（單位：億元，淨 = 買 - 賣）。

    回 [{date, foreign, trust, dealer}]（依日期遞增），正=買超、負=賣超。
    自營 = Dealer_self + Dealer_Hedging（自行 + 避險）。
    """
    key = f"mkt_inst:{days}"
    c = _cached(key)
    if c is not None:
        return c
    rows = _finmind("TaiwanStockTotalInstitutionalInvestors", days)
    by_date: dict[str, dict] = {}
    for r in rows:
        d = r.get("date")
        name = r.get("name")
        if not d or name in (None, "total"):
            continue
        net_yi = _net(r.get("buy"), r.get("sell")) / 1e8  # 元 → 億
        slot = by_date.setdefault(d, {"date": d, "foreign": 0.0, "trust": 0.0, "dealer": 0.0})
        if name in ("Foreign_Investor", "Foreign_Dealer_Self"):
            slot["foreign"] += net_yi
        elif name == "Investment_Trust":
            slot["trust"] += net_yi
        elif name in ("Dealer_self", "Dealer_Hedging"):
            slot["dealer"] += net_yi
    out = [{"date": d,
            "foreign": round(v["foreign"], 2),
            "trust": round(v["trust"], 2),
            "dealer": round(v["dealer"], 2)}
           for d, v in sorted(by_date.items())][-days:]
    return _store(key, out)


def fetch_pc_ratio(days: int = 20) -> list[dict]:
    """選擇權 Put/Call 比（TAIFEX 官方 CSV）。

    回 [{date, pc_vol, pc_oi}]（依日期遞增）：
    - pc_vol：賣權/買權「成交量」比 %
    - pc_oi ：賣權/買權「未平倉量」比 %（較常被當情緒指標）
    """
    key = f"pc:{days}"
    c = _cached(key)
    if c is not None:
        return c
    out: list[dict] = []
    try:
        r = requests.get(_TAIFEX_PC, timeout=15)
        text = r.content.decode("big5", errors="ignore")
        for line in text.splitlines():
            parts = line.split(",")
            if len(parts) < 7:
                continue
            raw_date = parts[0].strip()
            # 只取數據列（日期形如 2026/06/24）
            if not (len(raw_date) == 10 and raw_date[4] == "/"):
                continue
            try:
                date = raw_date.replace("/", "-")
                pc_vol = float(parts[3])
                pc_oi = float(parts[6])
            except (ValueError, IndexError):
                continue
            out.append({"date": date, "pc_vol": pc_vol, "pc_oi": pc_oi})
    except Exception as e:
        logger.warning("TAIFEX P/C 抓取失敗: %s", e)
        return []
    out.sort(key=lambda x: x["date"])
    out = out[-days:]
    return _store(key, out)


def fetch_index_close(days: int = 20, symbol: str = "^TWII") -> list[dict]:
    """大盤收盤序列（給趨勢圖疊圖用）。回 [{date, close}]（依日期遞增）。"""
    key = f"idx:{symbol}:{days}"
    c = _cached(key)
    if c is not None:
        return c
    out: list[dict] = []
    try:
        import yfinance as yf
        df = yf.download(symbol, start=_start_date(days), progress=False, auto_adjust=True)
        if df is not None and len(df) and "Close" in df:
            close = df["Close"]
            if hasattr(close, "ndim") and close.ndim > 1:
                close = close.iloc[:, 0]
            for idx, val in close.dropna().items():
                out.append({"date": str(idx.date()), "close": round(float(val), 2)})
    except Exception as e:
        logger.warning("大盤指數 %s 抓取失敗: %s", symbol, e)
        return []
    out = out[-days:]
    return _store(key, out)


def _strip_code(symbol: str) -> str:
    return symbol.split(".")[0].strip().upper()


def fetch_securities_lending(code: str, days: int = 20) -> list[dict]:
    """個股借券成交量（按日彙總；張）。借券量大 = 潛在空方壓力。"""
    key = f"sl:{code}:{days}"
    c = _cached(key)
    if c is not None:
        return c
    rows = _finmind("TaiwanStockSecuritiesLending", days, data_id=_strip_code(code))
    by_date: dict[str, float] = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        by_date[d] = by_date.get(d, 0.0) + (r.get("volume") or 0)
    # 注意：此 dataset 的 volume 已是「張」（2330 單日 ~13000 張），不可再 ÷1000
    out = [{"date": d, "lending_lots": int(round(by_date[d]))} for d in sorted(by_date)][-days:]
    return _store(key, out)


def fetch_day_trading(code: str, days: int = 20) -> list[dict]:
    """個股當沖（每日當沖量[張] + 當沖買賣超[億元]）。"""
    key = f"dt:{code}:{days}"
    c = _cached(key)
    if c is not None:
        return c
    rows = _finmind("TaiwanStockDayTrading", days, data_id=_strip_code(code))
    out = []
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        out.append({
            "date": d,
            "dt_lots": round((r.get("Volume") or 0) / 1000, 1),
            "dt_net_yi": round(((r.get("BuyAmount") or 0) - (r.get("SellAmount") or 0)) / 1e8, 2),
        })
    out.sort(key=lambda x: x["date"])
    out = out[-days:]
    return _store(key, out)


def stock_overview(symbol: str, days: int = 20) -> dict:
    """個股層級籌碼揭露彙整。純資料、不含任何買賣建議。

    重用 chipflow.fetch_chip_summary（三大法人連買天數 + 融資融券）、
    large_holder.get_large_holder_info（大戶 %），新增借券 + 當沖。
    """
    code = _strip_code(symbol)
    result: dict = {"code": code, "days": days}

    # 三大法人連買 + 融資融券（重用 chipflow）
    try:
        from layers.chipflow import fetch_chip_summary
        cs = fetch_chip_summary(symbol if "." in symbol else f"{code}.TW", days) or {}
        result["institutional"] = {
            "foreign_consec_buy": cs.get("foreign_consec_buy"),
            "trust_consec_buy": cs.get("trust_consec_buy"),
            # 三大法人買賣超是「股」→ ÷1000 換張；TWSE 融資/融券「已是張」→ 不可再除
            "foreign_total_net_lots": _lots(cs.get("foreign_total_net")),
            "trust_total_net_lots": _lots(cs.get("trust_total_net")),
            "dealer_total_net_lots": _lots(cs.get("dealer_total_net")),
            "margin_change_sum_lots": cs.get("margin_change_sum"),
            "short_balance_latest_lots": cs.get("short_balance_latest"),
            "daily": cs.get("daily_data") or [],
        }
    except Exception as e:
        logger.warning("個股法人彙總失敗 %s: %s", code, e)
        result["institutional"] = None

    # 大戶 %（重用 large_holder）
    try:
        from layers.large_holder import get_large_holder_info
        result["large_holder"] = get_large_holder_info(code)
    except Exception as e:
        logger.warning("大戶資訊失敗 %s: %s", code, e)
        result["large_holder"] = None

    result["securities_lending"] = fetch_securities_lending(code, days)
    result["day_trading"] = fetch_day_trading(code, days)
    result["notes"] = {
        "institutional": "三大法人連買天數（正=連買、負=連賣）+ 近N日累計（張）",
        "securities_lending": "借券成交量（張）；量大=潛在空方壓力",
        "day_trading": "當沖量（張）+ 當沖買賣超（億）",
        "large_holder": "集保大戶持股 %（>1000張）；籌碼集中度",
    }
    return result


def _lots(v):
    """股 → 張（÷1000）；None 原樣回。"""
    if v is None:
        return None
    try:
        return round(float(v) / 1000, 1)
    except (TypeError, ValueError):
        return None


def market_overview(days: int = 20) -> dict:
    """彙整三項市場層級籌碼揭露 + 大盤收盤。純資料、不含任何買賣建議。"""
    return {
        "days": days,
        "index": fetch_index_close(days),
        "futures_oi": fetch_futures_oi(days),
        "market_institutional": fetch_market_institutional(days),
        "pc_ratio": fetch_pc_ratio(days),
        "notes": {
            "futures_oi": "台指期三大法人未平倉淨額（口數）；正=淨多、負=淨空",
            "market_institutional": "大盤三大法人現貨買賣超（億元）；正=買超、負=賣超",
            "pc_ratio": "選擇權 Put/Call 比（%）；pc_oi=未平倉量比、pc_vol=成交量比",
        },
    }
