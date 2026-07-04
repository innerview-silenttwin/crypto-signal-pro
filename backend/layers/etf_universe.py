"""台股 ETF 兩份 Top10 榜（超選 universe 擴充 + 標籤用）。

1. 規模（AUM）Top10 — **人工建檔**（無免費 API；來源 Yahoo 股市資產規模排行、只取台股型）。
   規模榜變動慢，需要更新時重查改 `TOP_AUM_ETFS` + `AUM_ASOF` 即可。
2. 近半年贏大盤 Top10 — **每日自動計算**（FinMind 全上市台股型 ETF 6 個月報酬 vs TAIEX），
   disk 快取一天一算。槓桿/反向（L/R）不列入贏大盤比較（多頭正2必贏、不公平），
   上市不足半年者不列入（無完整半年基準）。

⚠️ 只作「超選 universe 擴充 + 顯示標籤」：
   - 不進 BEAT_ETFS（那是 alpha 持股評分權重、被動大型 ETF 會汙染訊號）
   - 不進 SECTOR_STOCKS 自動交易池（用戶 2026-07-04 決定）
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parents[1]
BEAT_CACHE_PATH = _BASE_DIR / "data" / "etf_beat_taiex.json"

# ── 榜一：台股 ETF 規模 Top10（人工建檔）───────────────────────
AUM_ASOF = "2026-07-04"   # 資料日期（Yahoo 股市 資產規模排行、台股型）
TOP_AUM_ETFS = [
    {"code": "0050",    "name": "元大台灣50",       "aum_billion": 22473},
    {"code": "0056",    "name": "元大高股息",       "aum_billion": 7228},
    {"code": "00878",   "name": "國泰永續高股息",   "aum_billion": 6140},
    {"code": "00919",   "name": "群益台灣精選高息", "aum_billion": 5351},
    {"code": "006208",  "name": "富邦台50",         "aum_billion": 4600},
    {"code": "00981A",  "name": "主動統一台股增長", "aum_billion": 2975},
    {"code": "00631L",  "name": "元大台灣50正2",    "aum_billion": 2509},
    {"code": "00403A",  "name": "主動統一升級50",   "aum_billion": 1738},
    {"code": "009816",  "name": "凱基台灣TOP50",    "aum_billion": 1637},
    {"code": "0052",    "name": "富邦科技",         "aum_billion": 1626},
]

# ── 榜二：近半年贏大盤 Top10（自動）───────────────────────────
_FM = "https://api.finmindtrade.com/api/v4/data"
LOOKBACK_DAYS = 183          # 近半年
MIN_HISTORY_DAYS = 165       # 上市須至少 ~5.5 個月才列入（無完整基準者不比）

# 非「台股型」名稱過濾（債券/海外/商品等）；槓桿反向另以代號尾碼 L/R 排除
_NON_TW_RE = re.compile(
    "債|美國|美元|標普|S&P|史坦普|納斯達|費城|道瓊|日本|日經|東證|越南|印度|中國|A50"
    "|滬|深證|恒生|香港|韓|歐洲|全球|國際|新興|亞太|亞洲|世界"   # 「韓」單字涵蓋 臺韓/台韓（00735 實跑抓到的漏洞）
    "|黃金|白銀|石油|原油|商品|期貨|REIT|地產|不動產"
)

# 名稱看不出海外、但實際以外股為主的（見 memory/project_active_etf_us：00988A/00990A
# 持股 NVDA/AAPL 等美股為主、當初也因此不進 BEAT_ETFS）
_EXPLICIT_EXCLUDE = {"00988A", "00990A"}


def _is_tw_equity_etf(stock_id: str, name: str) -> bool:
    """上市台股型 ETF 過濾：排除債券(B尾)、槓桿/反向(L/R尾)、海外/商品（名稱+顯式清單）。"""
    sid = stock_id.strip().upper()
    if sid.endswith(("B", "L", "R")):
        return False
    if sid in _EXPLICIT_EXCLUDE:
        return False
    if _NON_TW_RE.search(name or ""):
        return False
    return True


def _fm_get(dataset: str, **params) -> list:
    try:
        r = requests.get(_FM, params={"dataset": dataset, **params}, timeout=20)
        j = r.json()
        return j.get("data", []) if j.get("msg") == "success" else []
    except Exception as e:
        logger.warning("[etf_universe] FinMind %s 失敗: %s", dataset, e)
        return []


def _six_month_return(stock_id: str, start: str) -> float | None:
    """近半年報酬 %。上市不足（首筆晚於門檻）回 None。"""
    rows = _fm_get("TaiwanStockPrice", data_id=stock_id, start_date=start)
    closes = [(r["date"], r["close"]) for r in rows
              if r.get("close") not in (None, 0)]
    if len(closes) < 2:
        return None
    closes.sort()
    first_date = closes[0][0]
    threshold = (datetime.now() - timedelta(days=MIN_HISTORY_DAYS)).strftime("%Y-%m-%d")
    if first_date > threshold:      # 半年前沒掛牌 → 不列入
        return None
    base, last = closes[0][1], closes[-1][1]
    if not base:
        return None
    return (last / base - 1.0) * 100.0


def _compute_beat_taiex_top10() -> dict:
    """重算贏大盤榜（重：~百次 FinMind 呼叫、僅每日一次）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    taiex_ret = _six_month_return("TAIEX", start)
    if taiex_ret is None:
        logger.warning("[etf_universe] TAIEX 半年報酬取不到、放棄本次計算")
        return {"date": today, "taiex_ret": None, "top": []}

    info = _fm_get("TaiwanStockInfo")
    cands = [(r["stock_id"], r["stock_name"]) for r in info
             if r.get("type") == "twse" and r.get("industry_category") == "ETF"
             and _is_tw_equity_etf(r.get("stock_id", ""), r.get("stock_name", ""))]
    # 去重（TaiwanStockInfo 同代號可能多列）
    cands = list({sid: (sid, nm) for sid, nm in cands}.values())
    logger.info("[etf_universe] 台股型上市 ETF 候選 %d 檔、開始算半年報酬 vs TAIEX %.2f%%",
                len(cands), taiex_ret)

    scored = []
    for sid, nm in cands:
        ret = _six_month_return(sid, start)
        if ret is not None and ret > taiex_ret:
            scored.append({"code": sid, "name": nm, "ret6m_pct": round(ret, 2)})
    scored.sort(key=lambda x: -x["ret6m_pct"])
    top = scored[:10]
    logger.info("[etf_universe] 贏大盤 %d 檔、取前 10", len(scored))
    return {"date": today, "taiex_ret": round(taiex_ret, 2), "top": top}


def get_beat_taiex_top10(force: bool = False) -> dict:
    """近半年贏大盤 Top10（disk 快取、每日一算）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if not force:
        try:
            if BEAT_CACHE_PATH.exists():
                cached = json.loads(BEAT_CACHE_PATH.read_text())
                if cached.get("date") == today and cached.get("top"):
                    return cached
        except Exception:
            pass
    result = _compute_beat_taiex_top10()
    if result.get("top"):        # 失敗（空榜）不覆蓋快取、下次再試
        try:
            BEAT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            BEAT_CACHE_PATH.write_text(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            logger.warning("[etf_universe] 快取寫入失敗: %s", e)
    else:
        # 用舊快取頂著（若有）
        try:
            if BEAT_CACHE_PATH.exists():
                old = json.loads(BEAT_CACHE_PATH.read_text())
                if old.get("top"):
                    return old
        except Exception:
            pass
    return result


# ── 標籤 + universe 擴充 ────────────────────────────────────

def _strip(code: str) -> str:
    return code.replace(".TW", "").replace(".TWO", "").strip().upper()


def get_etf_tags(symbol: str) -> list[dict]:
    """回該代號所屬的榜單標籤（給超選顯示「屬於哪種前10」）。

    [{tag:"規模Top10", detail:"22,473億(2026-07-04)"}, {tag:"贏大盤Top10", detail:"+31.2% vs 大盤+18.0%"}]
    非榜內回 []。
    """
    code = _strip(symbol)
    tags = []
    for e in TOP_AUM_ETFS:
        if e["code"] == code:
            tags.append({"tag": "規模Top10",
                         "detail": f"{e['aum_billion']:,}億({AUM_ASOF})"})
            break
    beat = get_beat_taiex_top10()
    for e in beat.get("top", []):
        if e["code"] == code:
            tags.append({"tag": "贏大盤Top10",
                         "detail": f"半年{e['ret6m_pct']:+.1f}% vs 大盤{beat.get('taiex_ret'):+.1f}%"})
            break
    try:
        from layers.active_etf import BEAT_ETFS
        if any(b["code"] == code for b in BEAT_ETFS):
            tags.append({"tag": "主動嚴選", "detail": "alpha 領先清單成員"})
    except Exception:
        pass
    return tags


def get_universe_extension() -> dict:
    """兩榜聯集 → {"0050.TW": "元大台灣50", ...}，給超選 universe 擴充（不進交易池）。"""
    out: dict[str, str] = {}
    for e in TOP_AUM_ETFS:
        out[f"{e['code']}.TW"] = e["name"]
    for e in get_beat_taiex_top10().get("top", []):
        out.setdefault(f"{e['code']}.TW", e["name"])
    return out
