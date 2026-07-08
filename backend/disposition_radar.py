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
   A/B/C 為確定性、可信度高；D（30 日 12 次）因 TPEx 實際計數有除外/裁量，誤報較多。

資料源（皆免費）：TWSE 注意/處置 rwd/announcement/*（西元年）、TPEx 注意/處置
bulletin/*（民國年）、交易日曆 FinMind TAIEX。全走 http_legacy_ssl.legacy_get（保留
TLS 驗證、只關 OpenSSL 3.x 的 X509_STRICT，台灣政府憑證缺 SKI 會踩雷）。欄位一律用
回應的 `fields` 表頭以名稱定位（政府 JSON 偶爾增改欄位，硬編索引會靜默錯位）。
抓取失敗時回 degraded=True 且不快取（遵 cache 原則：失敗要重試而非 silent stale）。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from http_legacy_ssl import legacy_get

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
_RELIABLE_RULES = frozenset({"A", "B", "C"})   # D（30日12次）誤報多，標「參考」

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


# ── 解析工具（純函式、可測）─────────────────────────────────

def clause_nums(text: str) -> set[int]:
    """從注意資訊文字抓所有「第X款」的款次數字。"""
    return {_CN[m] for m in re.findall(r"第([一二三四五六七八九十]+)款", text or "") if m in _CN}


def norm_date(s: str) -> str | None:
    """民國/西元、以 / . - 分隔 → YYYY-MM-DD。無法解析回 None。"""
    if not s:
        return None
    m = re.search(r"(\d{2,4})[/.\-](\d{1,2})[/.\-](\d{1,2})", str(s))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1911:                 # 民國年
        y += 1911
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_period(s: str) -> tuple[str, str] | None:
    """處置起迄字串（含兩個日期）→ (start, end) 西元。抓不到兩個日期回 None。"""
    m = re.findall(r"(\d{2,4})[/.\-](\d{1,2})[/.\-](\d{1,2})", str(s or ""))
    if len(m) < 2:
        return None
    def g(t):
        y, mo, d = int(t[0]), int(t[1]), int(t[2])
        if y < 1911:
            y += 1911
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return g(m[0]), g(m[1])


def _col(fields: list, *needles) -> int | None:
    """在 fields 表頭找欄位索引：先精確、再包含子字串。找不到回 None。"""
    if not fields:
        return None
    for n in needles:
        for i, f in enumerate(fields):
            if str(f).strip() == n:
                return i
    for n in needles:
        for i, f in enumerate(fields):
            if n in str(f):
                return i
    return None


def _cell(row, idx):
    """安全取欄位；idx 為 None 或越界回 ''。"""
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def _schema_ok(data: list, fields: list) -> bool:
    """schema 健檢：有資料卻定位不到「證券代號」欄 → 視為格式改版/抓取異常。
    （避免欄位表頭改版時每列靜默略過、卻不標 degraded 的假『全清』。）"""
    if not data:
        return True                       # 真的沒資料（假日/無公告）不算異常
    return _col(fields, "證券代號", "代號") is not None


def parse_notice_rows(data: list, fields: list, market: str) -> list[tuple]:
    """注意公告 rows → [(code, name, date_greg, clause_set, market)]（普通股 4 碼）。
    以 fields 表頭定位欄位、短列/怪列略過，不硬編索引。"""
    ci = _col(fields, "證券代號", "代號")
    ni = _col(fields, "證券名稱", "名稱")
    di = _col(fields, "日期", "公告日期")
    ti = _col(fields, "注意交易資訊", "注意交易", "交易資訊")
    out = []
    for row in data:
        try:
            code = str(_cell(row, ci)).split("(")[0].strip()
            if not re.fullmatch(r"\d{4}", code):
                continue
            dt = norm_date(str(_cell(row, di)))
            if not dt:
                continue
            out.append((code, str(_cell(row, ni)).split("(")[0].strip(), dt,
                        clause_nums(str(_cell(row, ti))), market))
        except Exception:
            continue
    return out


def parse_disposal_rows(data: list, fields: list) -> list[tuple]:
    """處置公告 rows → [(code, (start,end))]（普通股 4 碼）。以 fields 表頭定位。"""
    ci = _col(fields, "證券代號", "代號")
    pi = _col(fields, "處置起迄時間", "處置起訖時間", "起迄", "起訖")
    out = []
    for row in data:
        try:
            code = str(_cell(row, ci)).split("(")[0].strip()
            if not re.fullmatch(r"\d{4}", code):
                continue
            p = parse_period(str(_cell(row, pi)))
            if p:
                out.append((code, p))
        except Exception:
            continue
    return out


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
    distance 夾在 0 以上（負值代表門檻已達、理應已被處置，夾為 0）。每條 reason 附
    reliable（A/B/C 確定性高=True、D=False）。
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
    raw = []
    if consec_first >= 1:
        d = _R_CONSEC_FIRST - consec_first
        cand.append(d); raw.append(("A", f"連{consec_first}天第一款", d))
    if consec_18 >= 1:
        d = _R_CONSEC_18 - consec_18
        cand.append(d); raw.append(("B", f"連{consec_18}天1-8款", d))
    if in10 >= 1:
        d = _R_IN10 - in10
        cand.append(d); raw.append(("C", f"10日內{in10}次", d))
    if in30 >= 1:
        d = _R_IN30 - in30
        cand.append(d); raw.append(("D", f"30日內{in30}次", d))
    if not cand:
        return {"distance": None, "tier": None, "counts": counts, "reasons": []}

    distance = max(0, min(cand))
    tier = "red" if distance <= 1 else ("orange" if distance == 2 else
                                        ("yellow" if distance == 3 else None))
    reasons = [{"rule": r, "text": t, "left": max(0, left),
                "reliable": r in _RELIABLE_RULES}
               for r, t, left in sorted(raw, key=lambda x: x[2]) if left <= 3]
    return {"distance": distance, "tier": tier, "counts": counts, "reasons": reasons}


# ── 網路抓取 ────────────────────────────────────────────────

def _get_json(url: str, params: dict) -> tuple:
    """回 (data_list, fields_list, ok)。任何失敗 ok=False（供上層判 degraded）。"""
    try:
        r = legacy_get(url, params=params, headers=_UA, timeout=30)
        if r.status_code != 200:
            logger.warning("[disposition_radar] %s HTTP %d", url, r.status_code)
            return [], [], False
        j = r.json()
        if isinstance(j, dict) and "tables" in j:      # TPEx bulletin
            t = (j.get("tables") or [{}])[0]
            return t.get("data") or [], t.get("fields") or [], True
        return j.get("data") or [], j.get("fields") or [], True   # TWSE rwd / FinMind
    except Exception as e:
        logger.warning("[disposition_radar] %s 抓取失敗: %s", url, e)
        return [], [], False


def _greg_range(bdays: int) -> tuple[str, str]:
    sd = (datetime.now() - timedelta(days=bdays * 2 + 10)).strftime("%Y%m%d")
    return sd, datetime.now().strftime("%Y%m%d")


def _roc_range(bdays: int) -> tuple[str, str]:
    def to_roc(dt):
        return f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"
    now = datetime.now()
    return to_roc(now - timedelta(days=bdays * 2 + 10)), to_roc(now)


def _trading_calendar(bdays: int) -> tuple[list, bool]:
    """近 bdays 個交易日（升冪）；用 TAIEX 日期。回 (list, ok)。"""
    start = (datetime.now() - timedelta(days=bdays * 2 + 10)).strftime("%Y-%m-%d")
    data, _, ok = _get_json(_FINMIND, {"dataset": "TaiwanStockPrice",
                                       "data_id": "TAIEX", "start_date": start})
    cal = sorted(x["date"] for x in data if x.get("date"))
    return cal, ok


def _fetch_attention_history(bdays: int) -> tuple:
    """回 (byd_by_code, name, market, ok)。byd = {code: {date: clause set}}。
    ok 為兩市場「皆成功」；任一市場失敗即 False（上層判 degraded）。"""
    from collections import defaultdict
    byd: dict = defaultdict(lambda: defaultdict(set))
    name: dict = {}
    market: dict = {}
    gsd, ged = _greg_range(bdays)
    tw_data, tw_fields, tw_ok = _get_json(_TWSE_NOTICE, {"startDate": gsd, "endDate": ged, "response": "json"})
    rsd, red = _roc_range(bdays)
    tp_data, tp_fields, tp_ok = _get_json(_TPEX_ATTENTION, {"startDate": rsd, "endDate": red, "response": "json"})
    for code, nm, dt, clauses, mkt in (parse_notice_rows(tw_data, tw_fields, "TWSE")
                                       + parse_notice_rows(tp_data, tp_fields, "TPEx")):
        byd[code][dt] |= clauses
        name[code] = nm
        market[code] = mkt
    ok = tw_ok and tp_ok and _schema_ok(tw_data, tw_fields) and _schema_ok(tp_data, tp_fields)
    return byd, name, market, ok


def _fetch_disposition_periods(bdays: int) -> tuple:
    """回 ({code: [(start,end)...]}, ok)。含上市＋上櫃。"""
    from collections import defaultdict
    out: dict = defaultdict(list)
    gsd, ged = _greg_range(bdays)
    tw_data, tw_fields, tw_ok = _get_json(_TWSE_PUNISH, {"startDate": gsd, "endDate": ged, "response": "json"})
    rsd, red = _roc_range(bdays)
    tp_data, tp_fields, tp_ok = _get_json(_TPEX_DISPOSAL, {"startDate": rsd, "endDate": red, "response": "json"})
    for code, period in parse_disposal_rows(tw_data, tw_fields) + parse_disposal_rows(tp_data, tp_fields):
        out[code].append(period)
    ok = tw_ok and tp_ok and _schema_ok(tw_data, tw_fields) and _schema_ok(tp_data, tp_fields)
    return out, ok


def compute_radar(bdays: int = 30, today: str | None = None) -> dict:
    """主入口：算今日「距處置」觀察名單。（資料一律取當前最新；today 僅供期間比對/快取鍵）

    回 {as_of, calendar_last, candidates:[...], in_disposition:[codes], stats, degraded}。
    candidates 依 distance 升冪。已在處置中或已公告即將生效（end≥today）的股票放
    in_disposition、不列 candidates。任一資料源抓取失敗 → degraded=True 且不快取。
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"radar:{bdays}:{today}"
    hit = _cached(cache_key)
    if hit is not None:
        return hit

    cal, cal_ok = _trading_calendar(max(35, bdays + 5))
    byd_by_code, name, market, att_ok = _fetch_attention_history(bdays)
    periods, disp_ok = _fetch_disposition_periods(bdays)

    # 交易日曆用聯集：TAIEX 常延遲一天，直接用 TAIEX 會漏掉「當天」注意（正是要預警那天）。
    # 只併注意日期（皆 ≤ today 的觀測日）；不可併處置未來 end 日，否則日曆尾端跑到未來、
    # _streak_from_end 從未來日往回立即中斷。最後夾在 ≤ today 保險。
    extra = set()
    for byd in byd_by_code.values():
        extra |= set(byd)
    calendar = sorted(d for d in (set(cal) | extra) if d and d <= today)

    # 抓取健康度：日曆/注意/處置任一失敗 → 無法可靠評估，標 degraded、不快取。
    # 日曆(TAIEX)也要納入：全失敗時只剩注意日期的稀疏日曆會虛構連續天數 → 假紅燈。
    degraded = not (cal_ok and att_ok and disp_ok)

    # 只用「已結束（end < today）」的處置重置計數；end≥today（進行中或已公告未生效）
    # 的股票不能列為候選（避免未來生效處置把計數清空使其憑空消失）
    last_end = {}
    disposed_or_pending = set()
    for code, plist in periods.items():
        for start, end in plist:
            if end >= today:
                disposed_or_pending.add(code)
            elif code not in last_end or end > last_end[code]:
                last_end[code] = end

    candidates = []
    for code, byd in byd_by_code.items():
        if code in disposed_or_pending:
            continue
        res = distance_to_disposition({d: set(cs) for d, cs in byd.items()},
                                      calendar, reset_after=last_end.get(code))
        if res["distance"] is None or res["distance"] > 3:
            continue
        candidates.append({
            "code": code, "name": name.get(code, ""), "market": market.get(code, ""),
            "distance": res["distance"], "tier": res["tier"],
            "reasons": res["reasons"], "counts": res["counts"],
            "last_disp_end": last_end.get(code),
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
        "in_disposition": sorted(disposed_or_pending),
        "degraded": degraded,
        "sources": {"calendar": cal_ok, "attention": att_ok, "disposition": disp_ok},
        "stats": {"candidates": len(candidates), **tier_n,
                  "in_disposition": len(disposed_or_pending)},
    }
    if degraded:                 # 失敗不快取，下次重試（遵 cache 原則）
        return result
    return _store(cache_key, result)
