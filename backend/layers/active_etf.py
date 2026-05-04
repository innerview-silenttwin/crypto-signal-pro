"""
主動式 ETF 持股評分層

邏輯：
- 追蹤 8 支績效領先大盤(0050)的台股主動式 ETF
- 每日抓取各 ETF 持股（CMoney 公開 API，免登入）
- 計算每支台股的「機構認可分數」：
    score = Σ(ETF排名權重 × 該股在ETF中的持股比例%)
- 用百分位正規化至 0-100，確保評分公平性
- 每日快取，供 screener 呼叫
"""

import os
import re
import json
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# ── 領先大盤的主動式 ETF，依超額報酬排名（每季更新）──
# 評估基準：各 ETF 成立以來 vs 0050 同期績效（2026-04-10 更新）
# rank_weight: 第1名=8份, 第2名=7份...，正規化後確保加總=1
BEAT_ETFS = [
    {"code": "00981A", "name": "主動統一台股增長", "alpha": 44.7},  # rank 1
    {"code": "00994A", "name": "主動第一金台股優", "alpha": 23.9},  # rank 2
    {"code": "00995A", "name": "主動中信台灣卓越", "alpha": 17.6},  # rank 3
    {"code": "00992A", "name": "主動群益科技創新", "alpha": 15.1},  # rank 4
    {"code": "00991A", "name": "主動復華未來50",   "alpha": 12.2},  # rank 5
    {"code": "00985A", "name": "主動野村台灣50",   "alpha": 11.7},  # rank 6
    {"code": "00987A", "name": "主動台新優勢成長", "alpha":  9.0},  # rank 7
    {"code": "00980A", "name": "主動野村臺灣優選", "alpha":  8.0},  # rank 8
]

_N = len(BEAT_ETFS)
_total_rank_points = _N * (_N + 1) // 2  # 1+2+...+N
for _i, _etf in enumerate(BEAT_ETFS):
    _etf["rank_weight"] = (_N - _i) / _total_rank_points

# ── 快取路徑 ──
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_CACHE_FILE = os.path.join(_CACHE_DIR, "active_etf_scores.json")

# ── 進榜歷史快取路徑 ──
_RANK_HISTORY_FILE = os.path.join(_CACHE_DIR, "active_etf_rank_history.json")

# ── 運行時快取（dict 用 in-place update 保持模組引用一致）──
_scores_cache: dict = {}   # {stock_id: normalized_score(0-100)}
_names_cache: dict = {}    # {stock_id: stock_name}
_etf_count_cache: dict = {}  # {stock_id: int} 被幾檔 ETF 持有
_etf_holders_cache: dict = {}  # {stock_id: [etf_code, ...]} 被哪些 ETF 持有
_cache_date: Optional[date] = None
_cache_lock = threading.RLock()  # 保護多執行緒下的讀寫安全


def _get_guest_token() -> Optional[str]:
    """從 CMoney ETF 頁面取得 guest JWT token"""
    try:
        resp = requests.get(
            "https://www.cmoney.tw/etf/tw/00981A/fundholding",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
            timeout=15
        )
        jwts = re.findall(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', resp.text)
        return jwts[0] if jwts else None
    except Exception as e:
        logger.warning(f"[active_etf] 取得 token 失敗: {e}")
        return None


def _fetch_holdings(etf_code: str, token: str) -> dict:
    """取得某 ETF 的台股持股 {stock_id: (name, weight_pct)}"""
    try:
        resp = requests.get(
            "https://www.cmoney.tw/MobileService/ashx/GetDtnoData.ashx",
            params={
                "action": "getdtnodata",
                "DtNo": 59449513,
                "ParamStr": f"AssignID={etf_code};DTRange=1;",
                "FilterNo": "0",
            },
            headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        result = {}
        for r in resp.json().get("Data", []):
            if len(r) < 6:
                continue
            sid, sname, pct_str, unit = r[1], r[2], r[3], r[5]
            if unit != "股":
                continue
            if any(sid.startswith(p) for p in ["C_", "M_", "PFUR", "RDI", "RECV"]):
                continue
            try:
                result[sid] = (sname, float(pct_str))
            except ValueError:
                pass
        return result
    except Exception as e:
        logger.warning(f"[active_etf] 抓 {etf_code} 持股失敗: {e}")
        return {}


def refresh_active_etf_scores() -> bool:
    """重新抓取所有 ETF 持股，計算並快取各股票分數。回傳是否成功。"""
    global _cache_date

    logger.info("[active_etf] 開始更新主動 ETF 持股分數...")
    token = _get_guest_token()
    if not token:
        logger.error("[active_etf] 無法取得 token，跳過更新")
        return False

    # 並行撈各 ETF 持股
    raw_scores: dict = {}
    names: dict = {}
    etf_count: dict = {}  # 每支股票被幾檔 ETF 持有
    etf_holders: dict = {}  # {stock_id: [etf_code, ...]} 持有它的 ETF 清單

    # 依 BEAT_ETFS 的原始排序記錄，方便前端按重要性顯示
    etf_order = {etf["code"]: idx for idx, etf in enumerate(BEAT_ETFS)}

    with ThreadPoolExecutor(max_workers=len(BEAT_ETFS)) as executor:
        futures = {executor.submit(_fetch_holdings, etf["code"], token): etf for etf in BEAT_ETFS}
        for future in as_completed(futures):
            etf = futures[future]
            holdings = future.result()
            w = etf["rank_weight"]
            for sid, (sname, pct) in holdings.items():
                raw_scores[sid] = raw_scores.get(sid, 0.0) + w * pct
                names[sid] = sname
                etf_count[sid] = etf_count.get(sid, 0) + 1
                etf_holders.setdefault(sid, []).append(etf["code"])

    # 依 BEAT_ETFS 排名重新排序每支股票的 holders 清單
    for sid in etf_holders:
        etf_holders[sid].sort(key=lambda c: etf_order.get(c, 999))

    if not raw_scores:
        logger.warning("[active_etf] 未取得任何持股資料")
        return False

    # 百分位正規化
    sorted_stocks = sorted(raw_scores.items(), key=lambda x: x[1])
    n = len(sorted_stocks)
    normalized = {
        sid: round((rank_idx / (n - 1)) * 100, 1) if n > 1 else 50.0
        for rank_idx, (sid, _) in enumerate(sorted_stocks)
    }

    with _cache_lock:
        _scores_cache.clear()
        _scores_cache.update(normalized)
        _names_cache.clear()
        _names_cache.update(names)
        _etf_count_cache.clear()
        _etf_count_cache.update(etf_count)
        _etf_holders_cache.clear()
        _etf_holders_cache.update(etf_holders)
        _cache_date = date.today()

    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": str(_cache_date), "scores": normalized, "names": names,
                   "etf_count_per_stock": etf_count,
                   "etf_holders_per_stock": etf_holders,
                   "etf_count": len(BEAT_ETFS), "stock_count": n},
                  f, ensure_ascii=False, indent=2)

    logger.info(f"[active_etf] 更新完成，共 {n} 支台股被 {len(BEAT_ETFS)} 支主動 ETF 持有")
    return True


def _load_cache_from_disk() -> bool:
    """從磁碟讀取快取，回傳是否成功且資料是今天的。
    若資料過期但存在，仍載入作為 fallback（避免 API 失敗時完全無資料）。
    """
    global _cache_date
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cache_date = date.fromisoformat(data["date"])
        has_etf_count = "etf_count_per_stock" in data
        is_fresh = cache_date >= date.today() and has_etf_count

        # 若磁碟版與記憶體版同日，視為已讀過，不重載 dict 也不 log
        with _cache_lock:
            already_loaded = (_cache_date == cache_date and len(_scores_cache) > 0)
            if not already_loaded:
                _scores_cache.clear()
                _scores_cache.update(data["scores"])
                _names_cache.clear()
                _names_cache.update(data.get("names", {}))
                _etf_count_cache.clear()
                _etf_count_cache.update(data.get("etf_count_per_stock", {}))
                _etf_holders_cache.clear()
                _etf_holders_cache.update(data.get("etf_holders_per_stock", {}))
                _cache_date = cache_date

        if not already_loaded:
            if is_fresh:
                logger.info(f"[active_etf] 從磁碟載入快取（{cache_date}，{len(_scores_cache)} 支股票）")
            else:
                logger.info(f"[active_etf] 載入過期快取作為 fallback（{cache_date}，{len(_scores_cache)} 支股票）")
        return is_fresh
    except Exception as e:
        logger.warning(f"[active_etf] 磁碟快取讀取失敗: {e}")
        return False


# 防併發 refresh：去重同一秒內重複的 lazy refresh 請求
_refresh_in_progress = threading.Event()
_last_refresh_attempt: float = 0.0


def _ensure_cache():
    """確保快取是今天的，否則刷新

    refresh 由獨立背景 task 主導（main.py active_etf_refresh_worker），
    這裡只在以下情況才主動觸發：磁碟快取也過期、且 60 秒內沒人在 refresh。
    """
    import time as _time
    global _last_refresh_attempt

    with _cache_lock:
        if _cache_date == date.today() and _scores_cache:
            return

    # 嘗試從磁碟載入新鮮快取（背景任務剛刷完就直接讀到）
    if _load_cache_from_disk():
        return

    # 磁碟也過期 → 60 秒去重 + 鎖防止 ThreadPool 併發 refresh 雪崩
    now = _time.time()
    if _refresh_in_progress.is_set() or (now - _last_refresh_attempt < 60):
        return  # 別人正在刷或剛刷過，不重複
    _last_refresh_attempt = now
    _refresh_in_progress.set()
    try:
        refresh_active_etf_scores()
    finally:
        _refresh_in_progress.clear()


def get_active_etf_score(symbol: str) -> Optional[float]:
    """
    取得某股票的主動 ETF 評分（0-100），若未被任何 ETF 持有則回傳 None。
    symbol 可為 "2330.TW" 或 "2330"。
    """
    _ensure_cache()
    sid = symbol.replace(".TW", "").replace(".tw", "").strip()
    with _cache_lock:
        return _scores_cache.get(sid)


def get_active_etf_holders(symbol: str) -> list:
    """取得某股票被哪些主動 ETF 持有，回傳 [etf_code, ...]，依 BEAT_ETFS 排名排序。"""
    _ensure_cache()
    sid = symbol.replace(".TW", "").replace(".tw", "").strip()
    with _cache_lock:
        return list(_etf_holders_cache.get(sid, []))


def _load_rank_history() -> dict:
    """讀取進榜歷史 JSON"""
    try:
        with open(_RANK_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_rank_history(history: dict):
    """儲存進榜歷史 JSON"""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_RANK_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[active_etf] 進榜歷史存檔失敗: {e}")


_twii_trading_days_cache: list = []

def _get_trading_days_count(start_date: str, end_date: str) -> int:
    """計算台股實際開市工作天數（快取加權指數交易日避免重複呼叫）"""
    global _twii_trading_days_cache
    try:
        from datetime import datetime, timedelta
        import pandas as pd

        if not _twii_trading_days_cache:
            import yfinance as yf
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=365)
            twii = yf.Ticker("^TWII")
            hist = twii.history(start=start_dt.strftime("%Y-%m-%d"),
                                end=(end_dt + timedelta(days=1)).strftime("%Y-%m-%d"))
            if not hist.empty:
                _twii_trading_days_cache = [d.strftime("%Y-%m-%d") for d in hist.index]

        if _twii_trading_days_cache:
            count = sum(1 for d in _twii_trading_days_cache if start_date <= d <= end_date)
            if end_date not in _twii_trading_days_cache and pd.to_datetime(end_date).weekday() < 5:
                count += 1
            return max(1, count)
    except Exception:
        pass
    try:
        import pandas as pd
        return max(1, len(pd.bdate_range(start=start_date, end=end_date)))
    except Exception:
        return 1


def _update_etf_rank_history(symbols: list, today: str) -> dict:
    """
    更新主動 ETF 進榜歷史，回傳 {symbol: days_in_rank}
    """
    history = _load_rank_history()
    current_set = set(symbols)

    # 移除不再上榜的股票
    for sym in list(history.keys()):
        if sym not in current_set:
            del history[sym]

    # 新進榜的股票記錄今天
    for sym in symbols:
        if sym not in history:
            history[sym] = today

    # 計算天數
    days_map = {}
    for sym in symbols:
        first_seen = history.get(sym, today)
        try:
            days_map[sym] = max(1, _get_trading_days_count(first_seen, today))
        except Exception:
            days_map[sym] = 1

    _save_rank_history(history)
    return days_map


def get_active_etf_ranking() -> dict:
    """
    公開 API：取得完整排行資料，供 /api/active-etf-ranking 呼叫。
    回傳 {stocks: [...], etfs: [...], total: int, updated_at: str}
    """
    _ensure_cache()
    with _cache_lock:
        if not _scores_cache:
            return {"stocks": [], "etfs": [], "total": 0, "updated_at": "",
                    "message": "資料載入中，請稍後再試"}
        stocks = sorted(
            [{"symbol": sid, "name": _names_cache.get(sid, sid), "score": score,
              "etf_count": int(_etf_count_cache.get(sid, 0)),
              "etf_holders": list(_etf_holders_cache.get(sid, []))}
             for sid, score in _scores_cache.items()],
            key=lambda x: -x["score"]
        )
        updated_at = str(_cache_date) if _cache_date else ""
        is_stale = _cache_date is not None and _cache_date < date.today()

    # 計算進榜天數
    today = str(date.today())
    symbols = [s["symbol"] for s in stocks]
    days_map = _update_etf_rank_history(symbols, today)
    for s in stocks:
        s["days_in_rank"] = days_map.get(s["symbol"], 1)

    etfs = [
        {"code": e["code"], "name": e["name"], "alpha": e["alpha"],
         "rank_weight": round(e["rank_weight"], 3)}
        for e in BEAT_ETFS
    ]
    result = {"stocks": stocks, "etfs": etfs, "total": len(stocks), "updated_at": updated_at}
    if is_stale:
        result["message"] = f"顯示 {updated_at} 的快取資料（今日尚未更新成功）"
    return result


# 啟動時預載磁碟快取，避免第一次請求觸發網路更新
_load_cache_from_disk()
