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
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, List

import pandas as pd
import requests

# openapi.twse.com.tw 走 TWCA 簽出的 cert chain，intermediate 缺 Subject Key Identifier，
# Python 3.14 strict 模式會擋掉。改用容忍 legacy chain 的 session（保留 hostname/過期等其他驗證）。
from http_legacy_ssl import legacy_get

from .base import BaseLayer, LayerModifier, LayerRegistry

logger = logging.getLogger(__name__)


# ── 快取設定 ──

_inst_cache: Dict = {}       # 三大法人快取 per-symbol：{cache_key: {"data": {date: {...}}, "time": float}}
_inst_history: Dict = {}     # 三大法人全市場歷史：{"data": {date: {code: {...}}}}（與落地檔同結構）
_margin_cache: Dict = {}     # 融資融券快取
_chip_summary_cache: Dict = {}  # 彙整後籌碼摘要 per-symbol：{cache_key: {"data": summary, "time": float}}
CHIP_CACHE_TTL = 3600 * 4    # 4 小時


def latest_published_t86_date() -> str:
    """回傳「最近一次應已公布」的 T86 三大法人資料日期（YYYYMMDD）。

    TWSE T86 約 16:00 後公布當日資料。
    - 週一~五 16:00 後 → today
    - 週一~五 16:00 前 → 上一個工作日
    - 週末 → 上週五
    用於判斷 cached summary 的 latest_date 是否落後（落後就強制 refetch）。
    """
    import pytz
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    candidate = now.date()
    publish_cutoff = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now.weekday() < 5 and now >= publish_cutoff:
        return candidate.strftime("%Y%m%d")
    candidate = candidate - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate - timedelta(days=1)
    return candidate.strftime("%Y%m%d")

# 本地持久快取檔案（累積每日歷史資料）
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
_INST_HISTORY_FILE = os.path.join(_DATA_DIR, "chip_inst_history.json")
_MARGIN_HISTORY_FILE = os.path.join(_DATA_DIR, "chip_margin_history.json")
_openapi_margin_fetched = False  # 本次啟動是否已抓過 OpenAPI 融資融券

# API 重試冷卻：失敗後 5 分鐘內不重試，避免每輪都卡 timeout
_MARGIN_RETRY_COOLDOWN = 300
_margin_last_attempt: float = 0.0

# 三大法人（T86 / TPEx）冷卻與回補上限
_INST_RETRY_COOLDOWN = 300
_inst_last_attempt: float = 0.0
# 回補視窗以「平日」計算，中間的休市日只會存成空標記；
# 長假（如農曆年最多 ~9 個平日）會吃掉額度，所以視窗要比 30 個交易日寬
_INST_BACKFILL_DAYS = 40    # 本地歷史要湊滿的平日數（30d 欄位需要 30 個交易日）
_INST_FETCH_PER_RUN = 8     # 單次最多補幾天，避免一次打太多 request
_INST_HISTORY_KEEP = 45     # 落地檔保留天數（含休市日標記）
_INST_MAX_ERRORS_PER_RUN = 3  # 單次回補容忍幾天失敗才收手
# screener 用 ThreadPoolExecutor(max_workers=5) 並行掃描，會同時進 _ensure_inst_history：
# 沒有鎖的話 5 條執行緒各自抓同一批日期，且互相覆蓋彼此的結果
_inst_lock = threading.Lock()
# 每個日期是否已抓過 T86（上市）。存在 day dict 裡，避免只有上櫃資料的日期
# 被誤判為「已補齊」而永遠不再抓上市部分。code 不可能是底線開頭，不會撞名。
_T86_DONE = "_t86"
_TPEX_DONE = "_tpex"

# FinMind 只當最後備援：匿名呼叫會被 IP ban（403 + retry_after），
# 被 ban 後在冷卻期內完全不再嘗試，避免每檔股票都卡一次 timeout
_finmind_blocked_until: float = 0.0


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


def _preload_margin_cache():
    """啟動時從本地歷史檔預載融資融券快取，確保 API 失敗時仍有資料可用"""
    history = _load_history_file(_MARGIN_HISTORY_FILE)
    if history:
        _margin_cache["data"] = history
        _margin_cache["time"] = time.time()
        latest = max(history.keys()) if history else "N/A"
        logger.info(f"融資融券：從本地快取預載 {len(history)} 天（最新 {latest}）")


def _preload_inst_cache():
    """啟動時從本地歷史檔預載三大法人快取，確保 API 失敗時仍有資料可用"""
    history = _load_history_file(_INST_HISTORY_FILE)
    if history:
        _inst_history["data"] = history
        latest = max(history.keys()) if history else "N/A"
        logger.info(f"三大法人：從本地快取預載 {len(history)} 天（最新 {latest}）")


_preload_margin_cache()
_preload_inst_cache()


def _strip_tw(symbol: str) -> str:
    """2330.TW → 2330"""
    return symbol.replace(".TWO", "").replace(".TW", "")


def _get_trading_dates(days: int = 10) -> List[str]:
    """取得最近 N 個可能的交易日日期 (往前多抓幾天以跳過假日)

    掃描範圍要夠大才湊得到 N 個平日：N 個平日至少橫跨 N*7/5 個日曆天，
    原本只掃 days+10 天，days=40 時只回得到 36 個日期。
    """
    dates = []
    for days_ago in range(0, int(days * 7 / 5) + 10):
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
    global _finmind_blocked_until
    # 被 ban 的冷卻期內直接跳過（不然每檔都要卡一次 request）
    if time.time() < _finmind_blocked_until:
        return {}

    url = (
        f"https://api.finmindtrade.com/api/v4/data"
        f"?dataset=TaiwanStockInstitutionalInvestorsBuySell"
        f"&data_id={stock_id}&start_date={start_date}&end_date={end_date}"
    )
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            # 匿名呼叫量大會被 IP ban：{"msg":"ip banned","status":403,"retry_after":493}
            if resp.status_code in (403, 429):
                retry_after = 600
                try:
                    retry_after = int(resp.json().get("retry_after") or retry_after)
                except Exception:
                    pass
                _finmind_blocked_until = time.time() + retry_after
                logger.warning(
                    f"FinMind 匿名呼叫被擋 (HTTP {resp.status_code})，"
                    f"{retry_after}s 內不再嘗試；改用 TWSE/TPEx 本地歷史"
                )
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


def _t86_field_index(fields: List[str], exact: str, *keywords: str,
                     exclude: tuple = ()) -> Optional[int]:
    """在 T86 表頭找欄位位置：先全字比對，再退回關鍵字全含比對（欄名偶有空白差異）。

    exclude 用來擋掉「名字包含目標關鍵字、但語意不同」的欄位——表頭同時有
    「自營商買賣超股數」「外資自營商買賣超股數」「自營商買賣超股數(自行買賣)」，
    純關鍵字比對會命中錯的那個。
    """
    for i, f in enumerate(fields):
        if f.strip() == exact:
            return i
    for i, f in enumerate(fields):
        norm = f.replace(" ", "")
        if any(x in norm for x in exclude):
            continue
        if all(k.replace(" ", "") in norm for k in keywords):
            return i
    return None


_EMPTY_SETTLED_AFTER_DAYS = 2   # 空結果要幾天後才敢認定是休市


def _is_settled_empty_date(date_str: str) -> bool:
    """空結果是否可以定論為休市（而非尚未公布）。

    TWSE 對「休市日」與「當日尚未上架」回同一句話，只能靠日期新舊區分：
    超過 2 天還是空的，就不可能是還沒公布。
    """
    try:
        import pytz
        d = datetime.strptime(date_str, "%Y%m%d").date()
        today = datetime.now(pytz.timezone("Asia/Taipei")).date()
        return (today - d).days >= _EMPTY_SETTLED_AFTER_DAYS
    except Exception:
        return False


def _fetch_twse_t86(date_str: str) -> tuple:
    """抓 TWSE T86 指定日期的全市場三大法人買賣超（一次 request 拿 1300+ 檔）。

    取代原本「逐檔打 FinMind」：universe 有 75+ 檔時那會發 75+ 個 request，
    匿名 IP 會被 FinMind 擋（403 ip banned），導致外資/投信類別整批歸零。

    Returns: (state, {code: {...}})
        "ok"      有資料，寫入並標記完成
        "holiday" 確定休市（夠舊仍是空的），標記完成避免重抓
        "pending" 空的但太新，可能尚未公布 → 跳過這天、繼續補更舊的
        "error"   網路/HTTP/表頭解析失敗 → 停手，整批下次再來
    """
    url = (f"https://www.twse.com.tw/rwd/zh/fund/T86"
           f"?response=json&date={date_str}&selectType=ALLBUT0999")
    try:
        resp = legacy_get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return "error", {}
        body = resp.json()
        stat = str(body.get("stat") or "")
        if stat != "OK":
            # 不能用 stat 字串判斷「休市」還是「尚未公布」：實測交易日當天收盤後
            # 資料未上架時，回的是與週日完全相同的「很抱歉，沒有符合條件的資料!」。
            # 改用日期新舊判斷：夠舊還是空的才是真休市（那時一定早就公布了）；
            # 太新的空結果視為尚未公布，不標記、下次再抓，否則當天資料會被永久跳過。
            if _is_settled_empty_date(date_str):
                return "holiday", {}
            logger.info(f"T86 {date_str} 尚無資料（{stat}），可能尚未公布，稍後重試")
            return "pending", {}
        fields = body.get("fields") or []
        rows = body.get("data") or []
        i_code = _t86_field_index(fields, "證券代號", "代號")
        i_foreign = _t86_field_index(fields, "外陸資買賣超股數(不含外資自營商)", "外陸資", "買賣超")
        i_trust = _t86_field_index(fields, "投信買賣超股數", "投信", "買賣超")
        # 要的是自營商「合計」。表頭同時有「外資自營商買賣超股數」（不同法人）
        # 與「自營商買賣超股數(自行買賣)」「(避險)」（只是其中一條腿），都要排除
        i_dealer = _t86_field_index(fields, "自營商買賣超股數", "自營商", "買賣超",
                                    exclude=("外資", "自行買賣", "避險"))
        if i_code is None or i_foreign is None or i_trust is None:
            logger.warning(f"T86 表頭無法解析 ({date_str}): {fields[:6]}")
            return "error", {}

        result: Dict[str, dict] = {}
        for row in rows:
            code = str(row[i_code]).strip()
            if not code or len(code) > 6:
                continue
            foreign = _parse_int(row[i_foreign]) or 0
            trust = _parse_int(row[i_trust]) or 0
            dealer = (_parse_int(row[i_dealer]) or 0) if i_dealer is not None else 0
            result[code] = {
                "foreign_net": foreign, "trust_net": trust,
                "dealer_net": dealer, "total_net": foreign + trust + dealer,
            }
        return "ok", result
    except Exception as e:
        logger.warning(f"T86 三大法人抓取失敗 ({date_str}): {e}")
        return "error", {}




def _inst_history_snapshot() -> Dict[str, dict]:
    """在鎖內取歷史的淺層快照。

    _ensure_inst_history 的 trim 會在鎖內 pop 日期；讀取端若直接迭代同一個 dict，
    可能撞到 "dictionary changed size during iteration"。呼叫端有的包在
    `except Exception: pass` 裡，例外會被吞掉、讓整個過期防護靜默失效。
    """
    with _inst_lock:
        # 連內層 day dict 一起複製：呼叫端會迭代 rec，而 _backfill 會對同一個
        # day dict 做 update（一次塞 1300 檔會觸發 resize），只複製外層擋不住
        return {d: dict(rec) for d, rec in (_inst_history.get("data") or {}).items()}


def _fetch_tpex_insti_for_date(date_str: str) -> tuple:
    """抓 TPEx（上櫃）指定日期的全市場三大法人買賣超。

    openapi 只給最新一天，上櫃就只能一天一天累積；但這樣新裝機的上櫃股
    30 日欄位只有 1 天資料，而上市股有 30 天——screener 是跨標的做百分位
    正規化的，兩者基準不同並不公平。這支用可帶日期的端點做歷史回補。

    此端點欄位名稱是重複的（買進/賣出/買賣超 各法人一組），只能靠位置取值；
    欄位順序已與 openapi 當日資料全量交叉比對確認。

    Returns: 與 _fetch_twse_t86 相同的三態
    """
    roc = f"{int(date_str[:4]) - 1911}/{date_str[4:6]}/{date_str[6:]}"
    url = ("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
           f"?type=Daily&sect=EW&date={roc}&response=json")
    try:
        resp = legacy_get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return "error", {}
        tables = (resp.json() or {}).get("tables") or []
        rows = (tables[0].get("data") or []) if tables else []
        if not rows:
            return ("holiday" if _is_settled_empty_date(date_str) else "pending"), {}

        I_CODE, I_FOREIGN, I_TRUST, I_DEALER = 0, 4, 13, 22
        result: Dict[str, dict] = {}
        for row in rows:
            if len(row) <= I_DEALER:
                continue
            code = str(row[I_CODE]).strip()
            if not code or len(code) > 6:
                continue
            foreign = _parse_int(row[I_FOREIGN]) or 0
            trust = _parse_int(row[I_TRUST]) or 0
            dealer = _parse_int(row[I_DEALER]) or 0
            result[code] = {
                "foreign_net": foreign, "trust_net": trust,
                "dealer_net": dealer, "total_net": foreign + trust + dealer,
            }
        return "ok", result
    except Exception as e:
        logger.warning(f"TPEx 三大法人（{date_str}）抓取失敗: {e}")
        return "error", {}


def _ensure_inst_history():
    """補齊本地三大法人歷史（上市走 T86 逐日回補、上櫃走 TPEx 最新日累積）。

    失敗不阻塞：有多少用多少，下次再補。冷卻機制避免每輪都卡 timeout。
    """
    global _inst_last_attempt

    with _inst_lock:
        history = _inst_history.setdefault("data", {})
        published = latest_published_t86_date()
        wanted = [d for d in _get_trading_dates(_INST_BACKFILL_DAYS) if d <= published]
        # 判斷依據是「該日是否抓過該市場」而不是「該日是否存在」：
        # 只要有一邊先把日期建出來，用 `d not in history` 就再也不會補另一邊
        missing_t86 = [d for d in wanted if not history.get(d, {}).get(_T86_DONE)]
        missing_tpex = [d for d in wanted if not history.get(d, {}).get(_TPEX_DONE)]
        if not missing_t86 and not missing_tpex:
            return

        # 冷卻無條件生效（含 history 為空的冷啟動）：
        # 否則 T86 一失敗，整輪 75 檔會各自重試一次，
        # 還會全部掉進 FinMind 個股查詢 —— 正是造成 IP ban 的那個模式
        now = time.time()
        if now - _inst_last_attempt < _INST_RETRY_COOLDOWN:
            return
        _inst_last_attempt = now

        changed = False

        def _backfill(dates: list, fetcher, done_key: str, label: str) -> int:
            """逐日回補單一市場。

            dates 是新到舊排列。「今天尚未公布」只該跳過那一天（pending），
            不能因此停掉整批——否則每天收盤後到 TWSE 實際上架前的那段時間，
            以及冷啟動遇到休市日時，較舊的日期永遠補不到。
            只有 error（網路/HTTP/表頭壞掉）才停手。
            """
            nonlocal changed
            got = 0
            attempted = 0
            errors = 0
            for date_str in dates:
                if attempted >= _INST_FETCH_PER_RUN:
                    break
                state, day_data = fetcher(date_str)
                attempted += 1
                if state == "error":
                    errors += 1
                    # 不直接 break：dates 是新到舊，若最新那天固定失敗（該日 500、
                    # 表頭壞掉…），整批停手會讓較舊、其實抓得到的日期永遠補不到。
                    # 連續錯誤累積到上限才收手，避免資料源真的掛掉時狂打。
                    if errors >= _INST_MAX_ERRORS_PER_RUN:
                        break
                    continue
                if state == "pending":
                    continue            # 該日尚未公布 → 不標記，換更舊的補
                day = history.setdefault(date_str, {})
                day.update(day_data)
                day[done_key] = 1       # 休市日也標記，避免每次重抓
                changed = True
                if day_data:
                    got += 1
            if got:
                logger.info(f"三大法人（{label}）回補 {got} 天")
            return got

        n_twse = _backfill(missing_t86, _fetch_twse_t86, _T86_DONE, "上市")
        n_tpex = _backfill(missing_tpex, _fetch_tpex_insti_for_date, _TPEX_DONE, "上櫃")

        # changed 而非「有抓到資料」才存檔：整批都是休市日時也要把標記寫下去，
        # 否則重啟後又要重抓同一批，白白吃掉回補額度
        if changed:
            kept = set(sorted(history.keys(), reverse=True)[:_INST_HISTORY_KEEP])
            # 就地縮減，不重新綁定 _inst_history["data"]，
            # 避免其他執行緒手上的 alias 變成孤兒 dict 而丟失資料
            for d in [d for d in history if d not in kept]:
                history.pop(d, None)
            _save_history_file(_INST_HISTORY_FILE, history)
            logger.info(f"三大法人歷史已更新：上市 +{n_twse} 天 / 上櫃 +{n_tpex} 天，"
                        f"本地共 {len(history)} 天")


def fetch_institutional_for_stock(symbol: str, days: int = 10) -> Dict[str, dict]:
    """
    取得個股近 N 天的三大法人買賣超（TWSE T86 / TPEx，FinMind 為最後備援）

    Returns:
        {date_str: {"foreign_net": int, "trust_net": int, "dealer_net": int, "total_net": int}}
    """
    code = _strip_tw(symbol)
    cache_key = f"inst_{code}"

    # 記憶體快取（per-symbol time）
    # 失效條件：TTL 過期，或 cache 內最新日期落後於「應已公布」日期 → 強制 refetch
    entry = _inst_cache.get(cache_key)
    if entry:
        age = time.time() - entry.get("time", 0)
        if age < CHIP_CACHE_TTL:
            cached_data = entry.get("data") or {}
            cache_latest = max(cached_data.keys()) if cached_data else ""
            if cache_latest >= latest_published_t86_date():
                return cached_data

    # 全市場歷史（一天一 request，所有股票共用）
    _ensure_inst_history()
    history = _inst_history_snapshot()
    result = {d: rec[code] for d, rec in history.items() if code in rec}

    # 只有在「上市+上櫃當日快照都完整、卻仍查不到這檔」時才退回 FinMind 個股查詢
    # （例如興櫃、剛上市、指數代號）。只要有一邊沒抓到，缺資料就可能是資料源掛了
    # 而不是這檔真的沒有——那時逐檔打 FinMind 會直接把 IP 打到被 ban
    newest = max(history, default=None)
    snapshot_complete = bool(newest) and bool(history[newest].get(_T86_DONE)) \
        and bool(history[newest].get(_TPEX_DONE))
    if not result and snapshot_complete:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
        result = _fetch_finmind_institutional(code, start_date, end_date)

    if result:
        _inst_cache[cache_key] = {"data": result, "time": time.time()}
        return result

    # 全部失敗：回舊 cache（若有）避免畫面瞬間空白；下次仍會嘗試重抓
    if entry:
        return entry.get("data") or {}
    return {}


# ── TWSE 融資融券 ──

def _fetch_margin_openapi() -> tuple:
    """從 TWSE OpenAPI 抓最新一天融資融券
    Returns: (date_str, {code: {...}}) or (None, {})
    """
    url = "https://openapi.twse.com.tw/v1/marginTrading/MI_MARGN"
    try:
        resp = legacy_get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
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
    """確保融資融券有最新資料：API 成功 → 更新快取；API 失敗 → 用本地歷史（不阻塞）"""
    global _openapi_margin_fetched, _margin_last_attempt
    if _openapi_margin_fetched:
        return
    # 冷卻中不重試（避免每 5 分鐘輪詢都卡 25 秒 timeout）
    now = time.time()
    if now - _margin_last_attempt < _MARGIN_RETRY_COOLDOWN and _margin_cache.get("data"):
        return
    _margin_last_attempt = now

    api_date, result = _fetch_margin_openapi()
    if result and api_date:
        _openapi_margin_fetched = True  # 只在成功時標記
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
    else:
        logger.warning("融資融券 API 失敗，使用本地歷史快取（%d 天）",
                        len(_margin_cache.get("data", {})))


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
    # per-symbol TTL，且若 cached summary 的 latest_date 落後於應公布日就強制 refetch
    entry = _chip_summary_cache.get(cache_key)
    if entry:
        age = now - entry.get("time", 0)
        if age < CHIP_CACHE_TTL:
            cached_summary = entry.get("data") or {}
            cached_latest = cached_summary.get("latest_date", "")
            if cached_latest and cached_latest >= latest_published_t86_date():
                return cached_summary
            # else 落後 → fall through 重抓

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
        "daily_data": daily_data[:5],  # 前端顯示用（只取 5 天）
        "daily_data_full": daily_data,  # 完整 N 天，供後端逐日計算金額
    }

    # 存入快取
    _chip_summary_cache[cache_key] = {"data": summary, "time": now}

    return summary


def compute_chip_score(summary: dict, close_price: float = None) -> dict:
    """
    根據籌碼摘要計算籌碼分數 (0-100)

    子信號權重：
    - 外資連買天數＆金額 30%
    - 投信連買天數 25%
    - 自營商 10%
    - 融資餘額增減 20% (反向指標)
    - 融券餘額 15%

    Args:
        summary: fetch_chip_summary() 的回傳值
        close_price: 最新收盤價，用於將股數轉換為金額判斷門檻

    Returns:
        {"score": int, "sub_scores": {...}, "label": str, "advice": str}
    """
    if not summary:
        return {"score": 50, "label": "無數據", "advice": "無籌碼資料", "sub_scores": {}}

    # ── 1. 外資分數 (30%) ──
    # 6mo (80檔) 校準：原 5/3 門檻過嚴，大漲股 chip 跟不上（聯電 ret +149% chip 相關 -0.53）
    # 改為 4/2，急漲股能更快進入高分區
    fc = summary.get("foreign_consec_buy", 0)
    if fc >= 4:
        foreign_score = 90
    elif fc >= 2:
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

    # 累計金額加成（有收盤價時用金額門檻，更公平）
    ft = summary.get("foreign_total_net", 0)
    if close_price and close_price > 0:
        ft_amount = ft * close_price
        if ft_amount > 50_000_000:       # 累計買超 5 千萬元以上
            foreign_score = min(100, foreign_score + 10)
        elif ft_amount < -50_000_000:
            foreign_score = max(0, foreign_score - 10)
    else:
        if ft > 50000:
            foreign_score = min(100, foreign_score + 10)
        elif ft < -50000:
            foreign_score = max(0, foreign_score - 10)

    # 30 日 momentum 加成：持續性進場潮（解決連買天數抓不到的情境）
    foreign_30d = summary.get("foreign_30d_net", 0)
    if close_price and close_price > 0:
        f30_amount = foreign_30d * close_price
        if f30_amount > 200_000_000:        # 30 日累計買超 2 億以上 = 強力進場潮
            foreign_score = min(100, foreign_score + 8)
        elif f30_amount < -200_000_000:
            foreign_score = max(0, foreign_score - 8)

    # ── 2. 投信分數 (25%) ──
    # 同樣 5/3 → 4/2，加快反映急漲股的法人介入
    tc = summary.get("trust_consec_buy", 0)
    if tc >= 4:
        trust_score = 92   # 投信連買很強，選股精準
    elif tc >= 2:
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

    # ── 4. 融資增減分數 (20%) — 反向指標（已弱化）──
    # 融資減少 = 散戶離場 = 籌碼沉澱 = 好事
    # 融資暴增原本 = 散戶追高 = 風險，但 6mo 校準發現強多頭時融資跟漲是正常 confirmation，
    # 不該重扣（聯電 +149%、台積電 +63% 期間融資皆增加而被扣分）。降低懲罰強度
    mc = summary.get("margin_change_sum", 0)
    if mc < -5000:
        margin_score = 80   # 大減，籌碼沉澱
    elif mc < -1000:
        margin_score = 65
    elif mc < 1000:
        margin_score = 55   # 略偏正（中性偏好）
    elif mc < 5000:
        margin_score = 48
    else:
        margin_score = 38   # 融資暴增不再重扣到 20，改 38（中性偏負）

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

        # ── Staleness guard：法人資料落後太多個「應公布日」→ 不參與評分 ──
        # Why: 資料源失敗時會 fallback 到磁碟歷史，可能是上週資料。
        #     用 stale 法人資料做交易決定（買賣超方向已過時）會誤導，寧可關掉。
        # 用「落後幾個應公布交易日」而非日曆天：日曆天會把連假算進去，
        # 長假後第一個交易日（例如週五休市 → 週一資料最新只到週四）會被誤判成過舊，
        # 導致整個籌碼面被靜默移出綜合評分（aggregator 對 active=False 是直接 skip，
        # 不會留下任何訊息），而那天的資料其實是正常的。
        CHIP_MAX_STALE_SESSIONS = 3   # 落後幾個「實際有資料的交易日」
        CHIP_MAX_STALE_CALENDAR = 14  # 絕對上限：連假最長也不會超過，超過就是資料源壞了
        latest_date = summary.get("latest_date", "")
        if latest_date:
            try:
                from datetime import datetime as _dt
                import pytz as _pytz
                # 以「全市場歷史裡實際有資料的交易日」為基準，而不是平日或日曆天：
                # 平日會把休市日算進去，長假後第一個交易日就會被誤判成過舊
                market_days = sorted(
                    (d for d, rec in _inst_history_snapshot().items()
                     if any(not k.startswith("_") for k in rec)),
                    reverse=True,
                )
                if market_days:
                    sessions_behind = sum(1 for d in market_days if d > latest_date)
                else:
                    published = latest_published_t86_date()
                    sessions_behind = sum(
                        1 for d in _get_trading_dates(CHIP_MAX_STALE_SESSIONS + 6)
                        if latest_date < d <= published
                    )
                # latest_date 一律是 YYYYMMDD（T86 與 FinMind 兩條路徑都已正規化）；
                # 原本寫 "%Y-%m-%d" 會固定拋例外被下面的 except 吃掉 → 這個 guard 等於沒作用
                latest_dt = _dt.strptime(latest_date, "%Y%m%d").date()
                age_days = (_dt.now(_pytz.timezone("Asia/Taipei")).date() - latest_dt).days
                if sessions_behind > CHIP_MAX_STALE_SESSIONS or age_days > CHIP_MAX_STALE_CALENDAR:
                    return LayerModifier(
                        layer_name=self.name, active=False,
                        reason=(f"籌碼資料過舊（最新 {latest_date}，落後 {sessions_behind} 個"
                                f"交易日 / {age_days} 個日曆天），暫不參與評分"),
                        details={"latest_date": latest_date,
                                 "sessions_behind": sessions_behind, "age_days": age_days},
                    )
            except Exception:
                # 日期格式異常不擋，繼續走原流程（避免一個解析失敗 kill 整個層）
                pass

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
