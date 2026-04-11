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

# ── 運行時快取（dict 用 in-place update 保持模組引用一致）──
_scores_cache: dict = {}   # {stock_id: normalized_score(0-100)}
_names_cache: dict = {}    # {stock_id: stock_name}
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

    with ThreadPoolExecutor(max_workers=len(BEAT_ETFS)) as executor:
        futures = {executor.submit(_fetch_holdings, etf["code"], token): etf for etf in BEAT_ETFS}
        for future in as_completed(futures):
            etf = futures[future]
            holdings = future.result()
            w = etf["rank_weight"]
            for sid, (sname, pct) in holdings.items():
                raw_scores[sid] = raw_scores.get(sid, 0.0) + w * pct
                names[sid] = sname

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
        _cache_date = date.today()

    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": str(_cache_date), "scores": normalized, "names": names,
                   "etf_count": len(BEAT_ETFS), "stock_count": n},
                  f, ensure_ascii=False, indent=2)

    logger.info(f"[active_etf] 更新完成，共 {n} 支台股被 {len(BEAT_ETFS)} 支主動 ETF 持有")
    return True


def _load_cache_from_disk() -> bool:
    """從磁碟讀取快取，回傳是否成功且資料是今天的"""
    global _cache_date
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cache_date = date.fromisoformat(data["date"])
        if cache_date < date.today():
            return False
        with _cache_lock:
            _scores_cache.clear()
            _scores_cache.update(data["scores"])
            _names_cache.clear()
            _names_cache.update(data.get("names", {}))
            _cache_date = cache_date
        logger.info(f"[active_etf] 從磁碟載入快取（{cache_date}，{len(_scores_cache)} 支股票）")
        return True
    except Exception as e:
        logger.warning(f"[active_etf] 磁碟快取讀取失敗: {e}")
        return False


def _ensure_cache():
    """確保快取是今天的，否則刷新"""
    with _cache_lock:
        if _cache_date == date.today() and _scores_cache:
            return
    if not _load_cache_from_disk():
        refresh_active_etf_scores()


def get_active_etf_score(symbol: str) -> Optional[float]:
    """
    取得某股票的主動 ETF 評分（0-100），若未被任何 ETF 持有則回傳 None。
    symbol 可為 "2330.TW" 或 "2330"。
    """
    _ensure_cache()
    sid = symbol.replace(".TW", "").replace(".tw", "").strip()
    with _cache_lock:
        return _scores_cache.get(sid)


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
            [{"symbol": sid, "name": _names_cache.get(sid, sid), "score": score}
             for sid, score in _scores_cache.items()],
            key=lambda x: -x["score"]
        )
        updated_at = str(_cache_date) if _cache_date else ""

    etfs = [
        {"code": e["code"], "name": e["name"], "alpha": e["alpha"],
         "rank_weight": round(e["rank_weight"], 3)}
        for e in BEAT_ETFS
    ]
    return {"stocks": stocks, "etfs": etfs, "total": len(stocks), "updated_at": updated_at}


# 啟動時預載磁碟快取，避免第一次請求觸發網路更新
_load_cache_from_disk()
