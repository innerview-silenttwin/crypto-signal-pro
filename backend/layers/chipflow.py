"""
籌碼面分析層 (Chip Flow / Institutional Analysis Layer)

用 TWSE OpenAPI 抓取三大法人買賣超、融資融券餘額：
1. T86 三大法人買賣超日報 → 外資/投信/自營商 淨買賣
2. MI_MARGN 融資融券餘額 → 融資增減、融券餘額
3. 計算連續買賣超天數，推算籌碼集中度

資料來源：
- https://openapi.twse.com.tw/v1/fund/T86
- https://openapi.twse.com.tw/v1/marginTrading/MI_MARGN

歷史資料透過本地檔案快取累積（OpenAPI 僅回傳最新一天）
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, List

import pandas as pd
import requests

from .base import BaseLayer, LayerModifier, LayerRegistry

logger = logging.getLogger(__name__)


# ── 快取設定 ──

_inst_cache: Dict = {}       # 三大法人快取 {"data": {date_str: {code: {...}}}, "time": float}
_margin_cache: Dict = {}     # 融資融券快取
_chip_summary_cache: Dict = {}  # 彙整後的籌碼摘要 {"data": {code: {...}}, "time": float}
CHIP_CACHE_TTL = 3600 * 4    # 4 小時

# 本地持久快取檔案（累積每日歷史資料）
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
_INST_HISTORY_FILE = os.path.join(_DATA_DIR, "chip_inst_history.json")
_MARGIN_HISTORY_FILE = os.path.join(_DATA_DIR, "chip_margin_history.json")
_openapi_inst_fetched = False   # 本次啟動是否已抓過 OpenAPI 三大法人
_openapi_margin_fetched = False  # 本次啟動是否已抓過 OpenAPI 融資融券


def _load_history_file(filepath: str) -> Dict:
    """讀取本地歷史快取檔"""
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_history_file(filepath: str, data: Dict):
    """儲存本地歷史快取檔"""
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"儲存歷史快取失敗 ({filepath}): {e}")


def _strip_tw(symbol: str) -> str:
    """2330.TW → 2330"""
    return symbol.replace(".TW", "").replace(".TWO", "")


def _get_trading_dates(days: int = 10) -> List[str]:
    """取得最近 N 個可能的交易日日期 (往前多抓幾天以跳過假日)"""
    dates = []
    for days_ago in range(0, days + 10):
        d = datetime.now() - timedelta(days=days_ago)
        # 跳過週末
        if d.weekday() >= 5:
            continue
        dates.append(d.strftime("%Y%m%d"))
        if len(dates) >= days:
            break
    return dates


def _parse_int(val) -> Optional[int]:
    """解析含逗號的整數字串"""
    try:
        v = str(val).strip().replace(",", "").replace(" ", "")
        if not v or v == "-" or v == "--":
            return None
        return int(v)
    except (ValueError, AttributeError):
        return None


def _parse_float(val) -> Optional[float]:
    """解析含逗號的浮點數字串"""
    try:
        v = str(val).strip().replace(",", "").replace(" ", "")
        if not v or v == "-" or v == "--":
            return None
        return float(v)
    except (ValueError, AttributeError):
        return None


# ── 三大法人買賣超（FinMind API）──

def _fetch_finmind_institutional(stock_id: str, start_date: str, end_date: str) -> Dict[str, dict]:
    """
    從 FinMind API 抓取個股三大法人每日買賣超

    Args:
        stock_id: 股票代碼 (e.g. "2330")
        start_date: 起始日期 "YYYY-MM-DD"
        end_date: 結束日期 "YYYY-MM-DD"

    Returns:
        {date_str(YYYYMMDD): {"foreign_net": int, "trust_net": int, "dealer_net": int, "total_net": int}}
    """
    url = (
        f"https://api.finmindtrade.com/api/v4/data"
        f"?dataset=TaiwanStockInstitutionalInvestorsBuySell"
        f"&data_id={stock_id}&start_date={start_date}&end_date={end_date}"
    )
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return {}
        body = resp.json()
        if body.get("status") != 200 or not body.get("data"):
            return {}

        # 依日期彙總
        by_date: Dict[str, dict] = {}
        for row in body["data"]:
            dt = row["date"].replace("-", "")  # "2026-04-01" → "20260401"
            if dt not in by_date:
                by_date[dt] = {"foreign_net": 0, "trust_net": 0, "dealer_net": 0, "total_net": 0}
            net = (row.get("buy", 0) or 0) - (row.get("sell", 0) or 0)
            name = row.get("name", "")
            if name == "Foreign_Investor":
                by_date[dt]["foreign_net"] += net
            elif name == "Investment_Trust":
                by_date[dt]["trust_net"] += net
            elif name in ("Dealer_self", "Dealer_Hedging"):
                by_date[dt]["dealer_net"] += net
            # total = foreign + trust + dealer
            by_date[dt]["total_net"] = (
                by_date[dt]["foreign_net"] + by_date[dt]["trust_net"] + by_date[dt]["dealer_net"]
            )

        return by_date
    except Exception as e:
        logger.warning(f"FinMind 三大法人抓取失敗 ({stock_id}): {e}")
        return {}


def fetch_institutional_for_stock(symbol: str, days: int = 10) -> Dict[str, dict]:
    """
    取得個股近 N 天的三大法人買賣超（FinMind）

    Returns:
        {date_str: {"foreign_net": int, "trust_net": int, "dealer_net": int, "total_net": int}}
    """
    code = _strip_tw(symbol)
    cache_key = f"inst_{code}"

    # 記憶體快取
    cached = _inst_cache.get("data", {}).get(cache_key)
    if cached and time.time() - _inst_cache.get("time", 0) < CHIP_CACHE_TTL:
        return cached

    # 計算日期範圍
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")

    result = _fetch_finmind_institutional(code, start_date, end_date)
    if result:
        if "data" not in _inst_cache:
            _inst_cache["data"] = {}
        _inst_cache["data"][cache_key] = result
        _inst_cache["time"] = time.time()
        logger.info(f"三大法人 {code}: {len(result)} 天 (FinMind)")

    return result


# ── TWSE 融資融券 ──

def _fetch_margin_openapi() -> tuple:
    """從 TWSE OpenAPI 抓最新一天融資融券
    Returns: (date_str, {code: {...}}) or (None, {})
    """
    url = "https://openapi.twse.com.tw/v1/marginTrading/MI_MARGN"
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200 or not resp.text.strip():
            logger.warning(f"融資融券 OpenAPI HTTP {resp.status_code}")
            return None, {}
        data = resp.json()
        if not isinstance(data, list) or len(data) == 0:
            return None, {}
        result = {}
        api_date = None
        for row in data:
            code = row.get("股票代號", "").strip()
            if not code or len(code) > 6:
                continue
            if api_date is None:
                raw_date = row.get("日期", "")
                if raw_date and len(raw_date) >= 7:
                    try:
                        yr = int(raw_date[:3]) + 1911
                        api_date = f"{yr}{raw_date[3:]}"
                    except ValueError:
                        pass
            margin_balance = _parse_int(row.get("融資今日餘額", 0))
            margin_prev = _parse_int(row.get("融資前日餘額", 0))
            short_balance = _parse_int(row.get("融券今日餘額", 0))
            result[code] = {
                "margin_buy": _parse_int(row.get("融資買進", 0)) or 0,
                "margin_sell": _parse_int(row.get("融資賣出", 0)) or 0,
                "margin_balance": margin_balance or 0,
                "margin_prev": margin_prev or 0,
                "margin_change": (margin_balance or 0) - (margin_prev or 0),
                "short_sell": _parse_int(row.get("融券賣出", 0)) or 0,
                "short_buy": _parse_int(row.get("融券買進", 0)) or 0,
                "short_balance": short_balance or 0,
            }
        if not api_date:
            # 無法從 API 解析日期時，用最近一個工作日（避免假日日期當 key）
            d = datetime.now()
            while d.weekday() >= 5:
                d -= timedelta(days=1)
            api_date = d.strftime("%Y%m%d")
        if result:
            logger.info(f"融資融券資料已更新 (OpenAPI): {len(result)} 筆, 日期={api_date}")
        return api_date, result
    except Exception as e:
        logger.warning(f"融資融券 OpenAPI 抓取失敗: {e}")
        return None, {}


def _ensure_margin_openapi():
    """確保本次啟動已從 OpenAPI 抓過融資融券最新資料"""
    global _openapi_margin_fetched
    if _openapi_margin_fetched:
        return
    _openapi_margin_fetched = True

    api_date, result = _fetch_margin_openapi()
    if result and api_date:
        if "data" not in _margin_cache:
            _margin_cache["data"] = {}
        _margin_cache["data"][api_date] = result
        _margin_cache["time"] = time.time()
        # 存入持久快取
        history = _load_history_file(_MARGIN_HISTORY_FILE)
        history[api_date] = result
        sorted_dates = sorted(history.keys(), reverse=True)[:35]
        history = {d: history[d] for d in sorted_dates}
        _save_history_file(_MARGIN_HISTORY_FILE, history)


def fetch_twse_margin(date_str: str) -> Dict[str, dict]:
    """
    從快取/歷史取得指定日期的融資融券餘額

    Returns:
        {stock_code: {"margin_buy": int, ..., "margin_change": int, "short_balance": int, ...}}
    """
    _ensure_margin_openapi()

    cached = _margin_cache.get("data", {}).get(date_str)
    if cached is not None:
        return cached

    history = _load_history_file(_MARGIN_HISTORY_FILE)
    if date_str in history:
        if "data" not in _margin_cache:
            _margin_cache["data"] = {}
        _margin_cache["data"][date_str] = history[date_str]
        return history[date_str]

    return {}


# ── 多日彙整分析 ──

def fetch_chip_summary(symbol: str, days: int = 5) -> Optional[dict]:
    """
    彙整指定股票近 N 日的籌碼資料，計算連買天數、累計金額等

    Returns:
        {
            "foreign_consec_buy": int,  # 外資連續買超天數 (負=連賣超)
            "foreign_total_net": int,   # 外資近 N 日累計淨買賣
            "trust_consec_buy": int,    # 投信連續買超天數
            "trust_total_net": int,
            "dealer_total_net": int,
            "margin_change_sum": int,   # 融資近 N 日累計增減
            "short_balance_latest": int,# 最新融券餘額
            "short_change_sum": int,    # 融券近 N 日增減
            "foreign_30d_net": int,     # 外資近 30 日累計買賣超
            "trust_30d_net": int,       # 投信近 30 日累計買賣超
            "dealer_30d_net": int,      # 自營商近 30 日累計買賣超
            "margin_30d_change": int,   # 融資近 30 日累計增減
            "short_30d_change": int,    # 融券近 30 日增減（最新－30日前）
            "daily_data": list,         # 每日明細
        }
    """
    now = time.time()
    cache_key = f"{symbol}_{days}"
    cached = _chip_summary_cache.get("data", {}).get(cache_key)
    if cached and now - _chip_summary_cache.get("time", 0) < CHIP_CACHE_TTL:
        return cached

    code = _strip_tw(symbol)

    # 三大法人：一次抓 30 天（取 max，確保 30d 欄位有資料）
    inst_by_date = fetch_institutional_for_stock(symbol, max(days, 30))

    # 以 FinMind 回傳的日期為準（FinMind 只回傳實際交易日，自動略過假日與週末）
    all_dates = sorted(inst_by_date.keys(), reverse=True)[:30]
    trading_dates = all_dates[:days]  # 主分析用的 N 天

    # 融資融券：OpenAPI 只有最新一天，歷史從本地快取讀取（最多 35 天）
    _ensure_margin_openapi()

    # 建立 30 天完整資料（inst + margin）
    all_daily_data = []
    for date_str in all_dates:
        inst_row = inst_by_date.get(date_str, {})
        margin_row = fetch_twse_margin(date_str).get(code, {})
        all_daily_data.append({
            "date": date_str,
            "foreign_net": inst_row.get("foreign_net", 0) or 0,
            "trust_net": inst_row.get("trust_net", 0) or 0,
            "dealer_net": inst_row.get("dealer_net", 0) or 0,
            "total_net": inst_row.get("total_net", 0) or 0,
            "margin_change": margin_row.get("margin_change", 0) or 0,
            "margin_balance": margin_row.get("margin_balance", 0) or 0,
            "short_balance": margin_row.get("short_balance", 0) or 0,
        })

    daily_data = all_daily_data[:days]  # 主分析用的 N 天

    if not daily_data:
        return None

    # 過濾掉當天三大法人全為 0 的資料（表示尚未收盤，不計入連續天數）
    effective_data = [
        d for d in daily_data
        if d["foreign_net"] != 0 or d["trust_net"] != 0 or d["dealer_net"] != 0
    ]
    # 如果只有融資融券的資料，還是保留 daily_data 做融資分析
    analysis_data = effective_data if effective_data else daily_data

    # 計算連續買超天數（從最近有效一天開始算）
    def _consec_days(data_list, key):
        """計算連續正數/負數天數，正=連買，負=連賣"""
        if not data_list:
            return 0
        first_val = data_list[0].get(key, 0)
        if first_val == 0:
            return 0
        direction = 1 if first_val > 0 else -1
        count = 0
        for d in data_list:
            val = d.get(key, 0)
            if (direction > 0 and val > 0) or (direction < 0 and val < 0):
                count += 1
            else:
                break
        return count * direction

    foreign_consec = _consec_days(analysis_data, "foreign_net")
    trust_consec = _consec_days(analysis_data, "trust_net")

    # 30 天統計（inst 只算有實際資料的交易日）
    inst_30d = [
        d for d in all_daily_data
        if d["foreign_net"] != 0 or d["trust_net"] != 0 or d["dealer_net"] != 0
    ]

    summary = {
        "foreign_consec_buy": foreign_consec,
        "foreign_total_net": sum(d["foreign_net"] for d in analysis_data),
        "trust_consec_buy": trust_consec,
        "trust_total_net": sum(d["trust_net"] for d in analysis_data),
        "dealer_total_net": sum(d["dealer_net"] for d in analysis_data),
        "margin_change_sum": sum(d["margin_change"] for d in daily_data),
        "short_balance_latest": daily_data[0]["short_balance"] if daily_data else 0,
        "short_change_sum": (
            daily_data[0]["short_balance"] - daily_data[-1]["short_balance"]
            if len(daily_data) > 1 else 0
        ),
        # 近 30 天統計
        "foreign_30d_net": sum(d["foreign_net"] for d in inst_30d),
        "trust_30d_net": sum(d["trust_net"] for d in inst_30d),
        "dealer_30d_net": sum(d["dealer_net"] for d in inst_30d),
        "margin_30d_change": sum(d["margin_change"] for d in all_daily_data),
        "short_30d_change": (
            all_daily_data[0]["short_balance"] - all_daily_data[-1]["short_balance"]
            if len(all_daily_data) > 1 else 0
        ),
        "latest_date": daily_data[0]["date"] if daily_data else "",
        "days_analyzed": len(daily_data),
        "days_30d_analyzed": len(all_daily_data),
        "daily_data": daily_data[:5],  # 只回傳最近 5 天明細給前端
    }

    # 存入快取
    if "data" not in _chip_summary_cache:
        _chip_summary_cache["data"] = {}
    _chip_summary_cache["data"][cache_key] = summary
    _chip_summary_cache["time"] = now

    return summary


def compute_chip_score(summary: dict) -> dict:
    """
    根據籌碼摘要計算籌碼分數 (0-100)

    子信號權重：
    - 外資連買天數＆金額 30%
    - 投信連買天數 25%
    - 自營商 10%
    - 融資餘額增減 20% (反向指標)
    - 融券餘額 15%

    Returns:
        {"score": int, "sub_scores": {...}, "label": str, "advice": str}
    """
    if not summary:
        return {"score": 50, "label": "無數據", "advice": "無籌碼資料", "sub_scores": {}}

    # ── 1. 外資分數 (30%) ──
    fc = summary.get("foreign_consec_buy", 0)
    if fc >= 5:
        foreign_score = 90
    elif fc >= 3:
        foreign_score = 75
    elif fc >= 1:
        foreign_score = 60
    elif fc == 0:
        foreign_score = 50
    elif fc >= -2:
        foreign_score = 35
    elif fc >= -4:
        foreign_score = 25
    else:
        foreign_score = 15

    # 累計金額加成
    ft = summary.get("foreign_total_net", 0)
    if ft > 50000:       # 累計買超 5 萬張以上
        foreign_score = min(100, foreign_score + 10)
    elif ft < -50000:
        foreign_score = max(0, foreign_score - 10)

    # ── 2. 投信分數 (25%) ──
    tc = summary.get("trust_consec_buy", 0)
    if tc >= 5:
        trust_score = 92   # 投信連買很強，選股精準
    elif tc >= 3:
        trust_score = 85
    elif tc >= 1:
        trust_score = 65
    elif tc == 0:
        trust_score = 50
    elif tc >= -2:
        trust_score = 30
    else:
        trust_score = 20

    # ── 3. 自營商分數 (10%) ──
    dt = summary.get("dealer_total_net", 0)
    if dt > 10000:
        dealer_score = 70
    elif dt > 0:
        dealer_score = 60
    elif dt == 0:
        dealer_score = 50
    elif dt > -10000:
        dealer_score = 40
    else:
        dealer_score = 30

    # ── 4. 融資增減分數 (20%) — 反向指標 ──
    # 融資減少 = 散戶離場 = 籌碼沉澱 = 好事
    # 融資暴增 = 散戶追高 = 風險
    mc = summary.get("margin_change_sum", 0)
    if mc < -5000:
        margin_score = 85   # 融資大減，籌碼沉澱
    elif mc < -1000:
        margin_score = 72
    elif mc < 1000:
        margin_score = 50   # 融資持平
    elif mc < 5000:
        margin_score = 35
    else:
        margin_score = 20   # 融資暴增，散戶追高

    # ── 5. 融券分數 (15%) ──
    sb = summary.get("short_balance_latest", 0)
    sc = summary.get("short_change_sum", 0)
    fc_val = summary.get("foreign_consec_buy", 0)

    if sb > 3000 and fc_val > 0:
        short_score = 85    # 高融券 + 外資買 = 軋空潛力
    elif sb > 3000:
        short_score = 60    # 高融券但無法人買
    elif sc > 1000:
        short_score = 55    # 融券增加中
    elif sb < 500:
        short_score = 50    # 融券低，中性
    else:
        short_score = 50

    # ── 加權計算總分 ──
    total_score = (
        foreign_score * 0.30 +
        trust_score * 0.25 +
        dealer_score * 0.10 +
        margin_score * 0.20 +
        short_score * 0.15
    )
    total_score = max(0, min(100, round(total_score)))

    # ── 標籤與建議 ──
    if total_score >= 80:
        label = "籌碼強烈偏多"
        advice = "法人積極買超，籌碼面強力支撐"
    elif total_score >= 65:
        label = "籌碼偏多"
        advice = "法人有進場跡象，籌碼面正向"
    elif total_score >= 50:
        label = "籌碼中性"
        advice = "法人動向不明確，觀察後續變化"
    elif total_score >= 35:
        label = "籌碼偏空"
        advice = "法人偏向賣出，籌碼面不利"
    else:
        label = "籌碼嚴重偏空"
        advice = "法人大幅賣超，不建議進場"

    return {
        "score": total_score,
        "label": label,
        "advice": advice,
        "sub_scores": {
            "foreign": {"score": foreign_score, "weight": 0.30,
                        "consec_days": summary.get("foreign_consec_buy", 0),
                        "total_net": summary.get("foreign_total_net", 0),
                        "net_30d": summary.get("foreign_30d_net", 0)},
            "trust": {"score": trust_score, "weight": 0.25,
                      "consec_days": summary.get("trust_consec_buy", 0),
                      "total_net": summary.get("trust_total_net", 0),
                      "net_30d": summary.get("trust_30d_net", 0)},
            "dealer": {"score": dealer_score, "weight": 0.10,
                       "total_net": summary.get("dealer_total_net", 0),
                       "net_30d": summary.get("dealer_30d_net", 0)},
            "margin": {"score": margin_score, "weight": 0.20,
                       "change_sum": summary.get("margin_change_sum", 0),
                       "change_30d": summary.get("margin_30d_change", 0)},
            "short": {"score": short_score, "weight": 0.15,
                      "balance": summary.get("short_balance_latest", 0),
                      "change_sum": summary.get("short_change_sum", 0),
                      "change_30d": summary.get("short_30d_change", 0)},
        },
    }


# ── Layer 類別 ──

class ChipFlowLayer(BaseLayer):
    """籌碼面分析層 — 三大法人 + 融資融券"""

    def __init__(self, enabled: bool = True, **kwargs):
        super().__init__("chipflow", enabled)

    def compute_modifier(self, symbol: str, df: pd.DataFrame,
                         sector_id: str = "") -> LayerModifier:
        if not self.enabled:
            return LayerModifier(layer_name=self.name, active=False,
                                 reason="籌碼面層未啟用")

        # 取得籌碼摘要
        summary = fetch_chip_summary(symbol)
        if not summary:
            return LayerModifier(
                layer_name=self.name, active=False,
                reason=f"{symbol} 無籌碼資料",
            )

        # 計算籌碼分數
        chip = compute_chip_score(summary)
        score = chip["score"]

        result = LayerModifier(layer_name=self.name)
        result.details = {
            "buy_score": score,
            "label": chip["label"],
            "advice": chip["advice"],
            "sub_scores": chip["sub_scores"],
            "foreign_consec_buy": summary.get("foreign_consec_buy", 0),
            "trust_consec_buy": summary.get("trust_consec_buy", 0),
            "margin_change_sum": summary.get("margin_change_sum", 0),
            "short_balance_latest": summary.get("short_balance_latest", 0),
            "latest_date": summary.get("latest_date", ""),
            "days_analyzed": summary.get("days_analyzed", 0),
            "days_30d_analyzed": summary.get("days_30d_analyzed", 0),
            "foreign_30d_net": summary.get("foreign_30d_net", 0),
            "trust_30d_net": summary.get("trust_30d_net", 0),
            "dealer_30d_net": summary.get("dealer_30d_net", 0),
            "margin_30d_change": summary.get("margin_30d_change", 0),
            "short_30d_change": summary.get("short_30d_change", 0),
            "daily_data": summary.get("daily_data", []),
        }

        # ── 根據分數設定修正器 ──
        if score >= 80:
            result.buy_multiplier = 1.25
            result.buy_offset = 6.0
            result.sell_multiplier = 0.7
            result.reason = f"籌碼強烈偏多（{score}分）：{chip['advice']}"
        elif score >= 65:
            result.buy_multiplier = 1.15
            result.buy_offset = 3.0
            result.sell_multiplier = 0.85
            result.reason = f"籌碼偏多（{score}分）：{chip['advice']}"
        elif score >= 50:
            result.buy_multiplier = 1.0
            result.sell_multiplier = 1.0
            result.reason = f"籌碼中性（{score}分）：{chip['advice']}"
        elif score >= 35:
            result.buy_multiplier = 0.85
            result.sell_multiplier = 1.1
            result.reason = f"籌碼偏空（{score}分）：{chip['advice']}"
        else:
            result.buy_multiplier = 0.65
            result.sell_multiplier = 1.25
            result.sell_offset = 5.0
            result.veto_buy = True
            result.reason = f"籌碼嚴重偏空（{score}分）：{chip['advice']}"

        # 特殊信號：外資+投信同步連買
        fc = summary.get("foreign_consec_buy", 0)
        tc = summary.get("trust_consec_buy", 0)
        if fc >= 3 and tc >= 3:
            result.buy_offset = min(result.buy_offset + 5.0, 15.0)
            result.reason += "｜外資+投信同步連買，籌碼高度集中"
            result.veto_sell = True  # 法人同步進場，不建議賣出

        return result


# 註冊到 LayerRegistry
LayerRegistry.register("chipflow", ChipFlowLayer)
