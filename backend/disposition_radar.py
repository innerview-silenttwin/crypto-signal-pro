"""處置雷達：預判「哪些股票即將達到處置條件」（純觀察、不做買賣判斷）。

用途：處置公告後最肥的一段跌幅發生在「公告當晚→隔日開盤」的跳空，公告後才進場吃不到；
只有「提前預判、公告前卡位」才抓得到。處置升級規則是確定性的，可用每日「注意」公告
累計情形反推「這檔再被注意幾次就進處置」。本模組只產生觀察名單與距離，不下任何單。

處置升級規則（TWSE/TPEx 作業要點第 6 條，達任一即處置）：
  A. 連續 3 個營業日 依「第一款」（累積漲跌幅）被公告注意
  B. 連續 5 個營業日 依「第一～八款」被公告注意
  C. 最近 10 個營業日內 6 日 依「第一～八款」
  D. 最近 30 個營業日內 12 日 依「第一～八款」
⚠️ 計數在「每次處置結束後歸零」——只計最近一次處置期結束後發生的注意（reset_after）。

資料源（皆免費、本地快取 + TTL）：
  TWSE  注意 rwd/announcement/notice、處置 rwd/announcement/punish（西元年）
  TPEx  注意 bulletin/attention、處置 bulletin/disposal（民國年，需 verify=False 繞 OpenSSL 3.x）
  交易日曆 FinMind TAIEX。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

_FINMIND = "https://api.finmindtrade.com/api/v4/data"
_TWSE_NOTICE = "https://www.twse.com.tw/rwd/zh/announcement/notice"
_TWSE_PUNISH = "https://www.twse.com.tw/rwd/zh/announcement/punish"
_TPEX_ATTENTION = "https://www.tpex.org.tw/www/zh-tw/bulletin/attention"
_TPEX_DISPOSAL = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
_UA = {"User-Agent": "Mozilla/5.0 (csp-disposition-radar)"}

_CACHE_TTL = 1800  # 30 分鐘
_cache: dict = {}

# 升級門檻
_R_CONSEC_FIRST = 3     # A：連續 N 天第一款
_R_CONSEC_18 = 5        # B：連續 N 天 1-8 款
_R_IN10 = 6             # C：最近 10 交易日 N 次
_R_IN30 = 12            # D：最近 30 交易日 N 次
_CLAUSE_18 = frozenset(range(1, 9))

_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
       "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14}


def _cached(key: str, ttl: int = _CACHE_TTL):
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    return None


def _store(key: str, data):
    _cache[key] = (time.time(), data)
    return data


# ── 解析工具（純函式）───────────────────────────────────────

def clause_nums(text: str) -> set[int]:
    """從注意資訊文字抓所有「第X款」的款次數字。"""
    return {_CN[m] for m in re.findall(r"第([一二三四五六七八九十]+)款", text or "") if m in _CN}


def norm_date(s: str) -> str | None:
    """民國/西元、以 / 或 . 分隔 → YYYY-MM-DD。無法解析回 None。"""
    if not s:
        return None
    s = str(s).strip().replace(".", "/")
    m = re.match(r"(\d{2,4})/(\d{1,2})/(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1911:                 # 民國年
        y += 1911
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_period(s: str) -> tuple[str, str] | None:
    """處置起迄字串（含兩個日期）→ (start, end) 西元。抓不到兩個日期回 None。"""
    m = re.findall(r"(\d{2,4})[/\.](\d{1,2})[/\.](\d{1,2})", str(s or ""))
    if len(m) < 2:
        return None
    def g(t):
        y, mo, d = int(t[0]), int(t[1]), int(t[2])
        if y < 1911:
            y += 1911
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return g(m[0]), g(m[1])


def _streak_from_end(hit_dates: set[str], calendar: list[str]) -> int:
    """交易日曆末端往回，連續命中的天數。"""
    s = 0
    for d in reversed(calendar):
        if d in hit_dates:
            s += 1
        else:
            break
    return s


def distance_to_disposition(byd: dict[str, set[int]], calendar: list[str],
                            reset_after: str | None = None) -> dict:
    """算「再被注意幾次就進處置」（純函式、可測）。

    byd: {注意日期: 該日款次集合}；calendar: 升冪交易日；reset_after: 最近一次處置結束日
    （只計晚於此日的注意，含歸零修正）。回 {distance, tier, counts, reasons}。
    distance 夾在 0 以上（負值代表門檻已達、理應已被處置，夾為 0）。
    """
    if reset_after:
        byd = {d: cs for d, cs in byd.items() if d > reset_after}
        calendar = [d for d in calendar if d > reset_after]
    if not byd:
        return {"distance": None, "tier": None, "counts": {}, "reasons": []}

    d_first = {d for d, cs in byd.items() if 1 in cs}
    d_18 = {d for d, cs in byd.items() if cs & _CLAUSE_18}
    last10 = set(calendar[-10:])
    last30 = set(calendar[-30:])

    consec_first = _streak_from_end(d_first, calendar)
    consec_18 = _streak_from_end(d_18, calendar)
    in10 = len(d_18 & last10)
    in30 = len(d_18 & last30)
    counts = {"consec_first": consec_first, "consec_18": consec_18,
              "in10": in10, "in30": in30}

    # 每條規則的「還差幾次」；未沾到該規則（0）視為不適用
    cand = []
    reasons = []
    if consec_first >= 1:
        dA = _R_CONSEC_FIRST - consec_first
        cand.append(dA); reasons.append(("A", f"連{consec_first}天第一款", dA))
    if consec_18 >= 1:
        dB = _R_CONSEC_18 - consec_18
        cand.append(dB); reasons.append(("B", f"連{consec_18}天1-8款", dB))
    if in10 >= 1:
        dC = _R_IN10 - in10
        cand.append(dC); reasons.append(("C", f"10日內{in10}次", dC))
    if in30 >= 1:
        dD = _R_IN30 - in30
        cand.append(dD); reasons.append(("D", f"30日內{in30}次", dD))
    if not cand:
        return {"distance": None, "tier": None, "counts": counts, "reasons": []}

    distance = max(0, min(cand))
    tier = "red" if distance <= 1 else ("orange" if distance == 2 else
                                        ("yellow" if distance == 3 else None))
    # 只留下逼近的規則說明（該規則本身 ≤3 才顯示）
    reasons = [{"rule": r, "text": t, "left": max(0, left)}
               for r, t, left in sorted(reasons, key=lambda x: x[2]) if left <= 3]
    return {"distance": distance, "tier": tier, "counts": counts, "reasons": reasons}


# ── 網路抓取 ────────────────────────────────────────────────

def _trading_calendar(bdays: int = 40) -> list[str]:
    """近 bdays 個交易日（升冪）；用 TAIEX 日期。抓失敗回空。"""
    start = (datetime.now() - timedelta(days=bdays * 2 + 10)).strftime("%Y-%m-%d")
    try:
        r = requests.get(_FINMIND, params={"dataset": "TaiwanStockPrice",
                                           "data_id": "TAIEX", "start_date": start}, timeout=20)
        data = r.json().get("data") or []
        cal = sorted(x["date"] for x in data)
        return cal[-bdays:]
    except Exception as e:
        logger.warning("[disposition_radar] 交易日曆抓取失敗: %s", e)
        return []


def _twse_json(url: str, sd: str, ed: str) -> list:
    try:
        r = requests.get(url, params={"startDate": sd, "endDate": ed, "response": "json"},
                         headers=_UA, timeout=30)
        return r.json().get("data") or []
    except Exception as e:
        logger.warning("[disposition_radar] TWSE %s 抓取失敗: %s", url, e)
        return []


def _tpex_json(url: str, sd: str, ed: str) -> list:
    try:
        # verify=False：TPEx 憑證缺 Subject Key Identifier，OpenSSL 3.x 會拒（公開資料）
        r = requests.get(url, params={"startDate": sd, "endDate": ed, "response": "json"},
                         headers=_UA, timeout=30, verify=False)
        return (r.json().get("tables") or [{}])[0].get("data") or []
    except Exception as e:
        logger.warning("[disposition_radar] TPEx %s 抓取失敗: %s", url, e)
        return []


def _greg_range(bdays: int) -> tuple[str, str]:
    sd = (datetime.now() - timedelta(days=bdays * 2 + 10)).strftime("%Y%m%d")
    ed = datetime.now().strftime("%Y%m%d")
    return sd, ed


def _roc_range(bdays: int) -> tuple[str, str]:
    def to_roc(dt):
        return f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"
    now = datetime.now()
    return to_roc(now - timedelta(days=bdays * 2 + 10)), to_roc(now)


def _fetch_attention_history(bdays: int) -> tuple[dict, dict, dict]:
    """回 (byd_by_code, name_by_code, market_by_code)。byd = {code: {date: clause set}}。"""
    from collections import defaultdict
    byd: dict = defaultdict(lambda: defaultdict(set))
    name: dict = {}
    market: dict = {}
    gsd, ged = _greg_range(bdays)
    for row in _twse_json(_TWSE_NOTICE, gsd, ged):
        code = str(row[1]).strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        dt = norm_date(str(row[5]))
        if not dt:
            continue
        byd[code][dt] |= clause_nums(str(row[4]))
        name[code] = str(row[2]).strip()
        market[code] = "TWSE"
    rsd, red = _roc_range(bdays)
    for row in _tpex_json(_TPEX_ATTENTION, rsd, red):
        code = str(row[1]).split("(")[0].strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        dt = norm_date(str(row[5]))
        if not dt:
            continue
        byd[code][dt] |= clause_nums(str(row[4]))
        name[code] = str(row[2]).split("(")[0].strip()
        market[code] = "TPEx"
    return byd, name, market


def _fetch_disposition_periods(bdays: int) -> dict:
    """回 {code: [(start,end), ...]}（近期處置期間，含上市＋上櫃）。"""
    from collections import defaultdict
    out: dict = defaultdict(list)
    gsd, ged = _greg_range(bdays)
    for row in _twse_json(_TWSE_PUNISH, gsd, ged):
        code = str(row[2]).strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        p = parse_period(str(row[6]))
        if p:
            out[code].append(p)
    rsd, red = _roc_range(bdays)
    for row in _tpex_json(_TPEX_DISPOSAL, rsd, red):
        code = str(row[2]).split("(")[0].strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        p = parse_period(str(row[5]))
        if p:
            out[code].append(p)
    return out


def compute_radar(bdays: int = 30, today: str | None = None) -> dict:
    """主入口：算今日「距處置」觀察名單。

    回 {as_of, calendar_last, candidates:[...], in_disposition:[codes], stats}。
    candidates 依 distance 升冪：{code,name,market,distance,tier,reasons,counts,last_disp_end}。
    已在處置中的股票放 in_disposition、不列 candidates。
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"radar:{bdays}:{today}"
    hit = _cached(cache_key)
    if hit is not None:
        return hit

    calendar = _trading_calendar(max(35, bdays + 5))
    byd_by_code, name, market = _fetch_attention_history(bdays)
    periods = _fetch_disposition_periods(bdays)

    active = set()
    last_end = {}
    for code, plist in periods.items():
        for start, end in plist:
            if start <= today <= end:
                active.add(code)
            if code not in last_end or end > last_end[code]:
                last_end[code] = end

    candidates = []
    for code, byd in byd_by_code.items():
        if code in active:
            continue
        # 計數重置：只計最近一次處置結束後的注意（處置起迄的 end 當日仍在處置，故 > end）
        reset = last_end.get(code)
        res = distance_to_disposition({d: set(cs) for d, cs in byd.items()},
                                      calendar, reset_after=reset)
        if res["distance"] is None or res["distance"] > 3:
            continue
        candidates.append({
            "code": code, "name": name.get(code, ""), "market": market.get(code, ""),
            "distance": res["distance"], "tier": res["tier"],
            "reasons": res["reasons"], "counts": res["counts"],
            "last_disp_end": reset,
        })
    candidates.sort(key=lambda x: (x["distance"], x["code"]))

    tier_n = {"red": 0, "orange": 0, "yellow": 0}
    for c in candidates:
        if c["tier"] in tier_n:
            tier_n[c["tier"]] += 1
    result = {
        "as_of": today,
        "calendar_last": calendar[-1] if calendar else None,
        "candidates": candidates,
        "in_disposition": sorted(active),
        "stats": {"candidates": len(candidates), **tier_n,
                  "in_disposition": len(active)},
    }
    return _store(cache_key, result)
