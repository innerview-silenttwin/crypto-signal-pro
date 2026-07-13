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

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from http_legacy_ssl import legacy_get

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
_ALERT_SEEN_PATH = _BASE_DIR / "data" / "disposition_alert_seen.json"
_ALERT_TTL_DAYS = 7          # 同一檔 7 天內不重推（避免每日洗版）

_FINMIND = "https://api.finmindtrade.com/api/v4/data"
_TWSE_NOTICE = "https://www.twse.com.tw/rwd/zh/announcement/notice"
_TWSE_PUNISH = "https://www.twse.com.tw/rwd/zh/announcement/punish"
_TPEX_ATTENTION = "https://www.tpex.org.tw/www/zh-tw/bulletin/attention"
_TPEX_DISPOSAL = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
_TWSE_MI_INDEX = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"      # 全市場單日
_TPEX_DAILY = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"   # 全市場單日
_UA = {"User-Agent": "Mozilla/5.0 (csp-disposition-radar)"}

# 漲多預警：6 日累積漲幅進入警戒帶（未列注意但接近 TWSE 第一款 ~32% 門檻）
_RISE_WARN_PCT = 25.0
_RISE_THRESHOLD_PCT = 32.0

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


def _clean_text(s: str) -> str:
    """注意/處置原文清理：<br> → 換行、去其他 HTML tag、壓空白。"""
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", str(s or ""), flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def parse_notice_rows(data: list, fields: list, market: str) -> list[tuple]:
    """注意公告 rows → [(code, name, date_greg, clause_set, market, raw_text)]（普通股 4 碼）。
    raw_text＝該筆注意原文（含實際漲幅/天數/價差/週轉率等數字，供 hover 顯示）。
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
            raw = str(_cell(row, ti))
            out.append((code, str(_cell(row, ni)).split("(")[0].strip(), dt,
                        clause_nums(raw), market, _clean_text(raw)))
        except Exception:
            continue
    return out


def _disposal_level(measure: str) -> str:
    """由處置內容文字判等級：加重(約20分盤) / 第一次(約5分盤) / 處置中。"""
    t = str(measure or "")
    if "第二次" in t or "第三次" in t or "加重" in t or "每二十分" in t or "每20分" in t or "二十分鐘" in t:
        return "加重(約20分盤)"
    if "第一次" in t or "每五分" in t or "每5分" in t or "五分鐘" in t:
        return "第一次(約5分盤)"
    return "處置中"


def parse_disposal_rows(data: list, fields: list) -> list[tuple]:
    """處置公告 rows → [(code, start, end, level, measure_text)]（普通股 4 碼）。
    measure_text＝處置內容原文（含分盤間隔/預收條件）；level＝由文字判定的等級。"""
    ci = _col(fields, "證券代號", "代號")
    pi = _col(fields, "處置起迄時間", "處置起訖時間", "起迄", "起訖")
    mi = _col(fields, "處置內容", "處置措施", "處置原因")
    out = []
    for row in data:
        try:
            code = str(_cell(row, ci)).split("(")[0].strip()
            if not re.fullmatch(r"\d{4}", code):
                continue
            p = parse_period(str(_cell(row, pi)))
            if not p:
                continue
            measure = _clean_text(_cell(row, mi))
            out.append((code, p[0], p[1], _disposal_level(measure), measure))
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
    """回 (byd_by_code, name, market, latest_notice, ok)。byd = {code: {date: clause set}}；
    latest_notice = {code: {date, text}}（該股最新一筆注意原文，含實際數字，供 hover）。
    ok 為兩市場「皆成功」；任一市場失敗即 False（上層判 degraded）。"""
    from collections import defaultdict
    byd: dict = defaultdict(lambda: defaultdict(set))
    name: dict = {}
    market: dict = {}
    latest: dict = {}
    gsd, ged = _greg_range(bdays)
    tw_data, tw_fields, tw_ok = _get_json(_TWSE_NOTICE, {"startDate": gsd, "endDate": ged, "response": "json"})
    rsd, red = _roc_range(bdays)
    tp_data, tp_fields, tp_ok = _get_json(_TPEX_ATTENTION, {"startDate": rsd, "endDate": red, "response": "json"})
    for code, nm, dt, clauses, mkt, raw in (parse_notice_rows(tw_data, tw_fields, "TWSE")
                                            + parse_notice_rows(tp_data, tp_fields, "TPEx")):
        byd[code][dt] |= clauses
        name[code] = nm
        market[code] = mkt
        if raw and (code not in latest or dt >= latest[code]["date"]):
            latest[code] = {"date": dt, "text": raw}
    ok = tw_ok and tp_ok and _schema_ok(tw_data, tw_fields) and _schema_ok(tp_data, tp_fields)
    return byd, name, market, latest, ok


def _fetch_disposition_periods(bdays: int) -> tuple:
    """回 (periods, detail, ok)。periods={code:[(start,end)...]}；
    detail={code:[{start,end,level,measure}...]}（供處置中明細表）。含上市＋上櫃。"""
    from collections import defaultdict
    periods: dict = defaultdict(list)
    detail: dict = defaultdict(list)
    gsd, ged = _greg_range(bdays)
    tw_data, tw_fields, tw_ok = _get_json(_TWSE_PUNISH, {"startDate": gsd, "endDate": ged, "response": "json"})
    rsd, red = _roc_range(bdays)
    tp_data, tp_fields, tp_ok = _get_json(_TPEX_DISPOSAL, {"startDate": rsd, "endDate": red, "response": "json"})
    for code, start, end, level, measure in (parse_disposal_rows(tw_data, tw_fields)
                                             + parse_disposal_rows(tp_data, tp_fields)):
        periods[code].append((start, end))
        detail[code].append({"start": start, "end": end, "level": level, "measure": measure})
    ok = tw_ok and tp_ok and _schema_ok(tw_data, tw_fields) and _schema_ok(tp_data, tp_fields)
    return periods, detail, ok


def _hovering_stats(byd: dict, dstarts_dends: list, calendar: list, window_days: int) -> dict:
    """一檔的「邊緣徘徊」統計（近 window_days 交易日內）：
      near_miss_days＝逐日重建後「距處置≤1（再1次）」的天數
      triggers＝實際進處置次數
      edge_hovering＝反覆逼近卻幾乎不跨線（≥3 天站上再1次、觸發≤1）——潛在貓膩，工具標型態、由人判斷。
    """
    window = set(calendar[-window_days:]) if calendar else set()
    near = 0
    for d in sorted(byd):
        if d not in window:
            continue
        reset = max((e for s, e in dstarts_dends if e < d), default=None)
        r = distance_to_disposition({k: set(v) for k, v in byd.items() if k <= d},
                                    [x for x in calendar if x <= d], reset_after=reset)
        if r["distance"] is not None and r["distance"] <= 1:
            near += 1
    triggers = len({s for s, _ in dstarts_dends if s in window})
    return {"near_miss_days": near, "triggers": triggers,
            "window_days": window_days,
            "edge_hovering": near >= 3 and triggers <= 1}


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
    byd_by_code, name, market, latest_notice, att_ok = _fetch_attention_history(bdays)
    periods, disp_detail, disp_ok = _fetch_disposition_periods(bdays)

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
    last_period = {}          # code -> (start, end) 最近一次「已結束」的處置
    disposed_or_pending = set()
    for code, plist in periods.items():
        for start, end in plist:
            if end >= today:
                disposed_or_pending.add(code)
            elif code not in last_period or end > last_period[code][1]:
                last_period[code] = (start, end)
    last_end = {c: p[1] for c, p in last_period.items()}

    candidates = []
    for code, byd in byd_by_code.items():
        if code in disposed_or_pending:
            continue
        res = distance_to_disposition({d: set(cs) for d, cs in byd.items()},
                                      calendar, reset_after=last_end.get(code))
        if res["distance"] is None or res["distance"] > 3:
            continue
        hov = _hovering_stats(byd, periods.get(code, []), calendar, bdays)
        candidates.append({
            "code": code, "name": name.get(code, ""), "market": market.get(code, ""),
            "distance": res["distance"], "tier": res["tier"],
            "reasons": res["reasons"], "counts": res["counts"],
            "last_disp_end": last_end.get(code), "hovering": hov,
            "last_disp_start": last_period.get(code, (None, None))[0],
            "latest_notice": latest_notice.get(code),
        })
    candidates.sort(key=lambda x: (x["distance"], x["code"]))

    # 處置中明細：取每檔「涵蓋 today」的處置期（出關日、措施、等級），附觸發時注意數字與可能再處置
    in_disposition = []
    for code in sorted(disposed_or_pending):
        recs = disp_detail.get(code, [])
        active = None
        for r in recs:
            if r["start"] <= today <= r["end"] or r["start"] > today:   # 進行中 或 已公告未生效
                if active is None or r["end"] > active["end"]:
                    active = r
        if active is None:
            active = max(recs, key=lambda r: r["end"], default={"start": None, "end": None,
                                                                "level": "處置中", "measure": ""})
        past_cnt = sum(1 for r in recs if r["end"] < today)     # 近期已結束處置次數
        re_risk = ("加重" in active["level"]) or past_cnt >= 1
        in_disposition.append({
            "code": code, "name": name.get(code, ""), "market": market.get(code, ""),
            "start": active["start"], "end": active["end"], "level": active["level"],
            "measure": active["measure"], "latest_notice": latest_notice.get(code),
            "prior_disposals": past_cnt, "re_risk": bool(re_risk),
            "pending": bool(active["start"] and active["start"] > today),
        })

    tier_n = {"red": 0, "orange": 0, "yellow": 0}
    for c in candidates:
        if c["tier"] in tier_n:
            tier_n[c["tier"]] += 1

    # 漲多預警（供更早觀察；補充性質、失敗不影響主雷達 degraded）
    try:
        rising = rising_radar(calendar, set(byd_by_code) | disposed_or_pending)
    except Exception as e:
        logger.warning("[disposition_radar] 漲多預警計算失敗: %s", e)
        rising = []

    result = {
        "as_of": today,
        "calendar_last": calendar[-1] if calendar else None,
        "candidates": candidates,
        "rising": rising,
        "in_disposition": in_disposition,
        "degraded": degraded,
        "sources": {"calendar": cal_ok, "attention": att_ok, "disposition": disp_ok},
        "stats": {"candidates": len(candidates), **tier_n,
                  "rising": len(rising),
                  "in_disposition": len(in_disposition)},
    }
    if degraded:                 # 失敗不快取，下次重試（遵 cache 原則）
        return result
    return _store(cache_key, result)


def _get_tables(url: str, params: dict) -> tuple:
    """多表端點（MI_INDEX/dailyQuotes）→ (tables_list, ok)。單表 rwd 也包成一張表。"""
    try:
        r = legacy_get(url, params=params, headers=_UA, timeout=30)
        if r.status_code != 200:
            return [], False
        j = r.json()
        if isinstance(j, dict) and "tables" in j:
            return j["tables"] or [], True
        return [{"fields": j.get("fields") or [], "data": j.get("data") or []}], True
    except Exception as e:
        logger.warning("[disposition_radar] %s 抓取失敗: %s", url, e)
        return [], False


def _parse_close(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _all_market_closes(date_greg: str) -> tuple:
    """某日全市場普通股(4碼)收盤 → ({code: (name, close, market)}, ok)。TWSE+TPEx。"""
    out = {}
    any_ok = False
    ymd = date_greg.replace("-", "")
    slashed = date_greg.replace("-", "/")
    for url, params, mkt in (
        (_TWSE_MI_INDEX, {"date": ymd, "type": "ALLBUT0999", "response": "json"}, "TWSE"),
        (_TPEX_DAILY, {"date": slashed, "response": "json"}, "TPEx"),
    ):
        tables, ok = _get_tables(url, params)
        any_ok = any_ok or ok
        for t in tables:
            f = t.get("fields") or []
            ci = _col(f, "證券代號", "代號")
            pi = _col(f, "收盤價", "收盤")
            if ci is None or pi is None:
                continue
            ni = _col(f, "證券名稱", "名稱")
            for row in t.get("data") or []:
                code = str(_cell(row, ci)).strip()
                if not re.fullmatch(r"\d{4}", code):
                    continue
                close = _parse_close(_cell(row, pi))
                if close:
                    out[code] = (str(_cell(row, ni)).strip() if ni is not None else "", close, mkt)
            break                              # 該市場找到價格表就好
    return out, any_ok


def rising_radar(calendar: list, exclude: set) -> list:
    """漲多預警：近 6 個交易日累積漲幅 ≥ 警戒帶、且尚未列注意/處置的普通股。

    ⚠️ 近似值：只算原始 6 日漲幅，未套 TWSE 第一款「與大盤/同類差幅」條件，僅供更早觀察。
    """
    if len(calendar) < 6:
        return []
    base, ok1 = _all_market_closes(calendar[-6])
    latest, ok2 = _all_market_closes(calendar[-1])
    if not (ok1 and ok2):
        return []
    rows = []
    for code, (name, close, mkt) in latest.items():
        if code in exclude:
            continue
        b = base.get(code)
        if not b or not b[1]:
            continue
        ret = (close / b[1] - 1) * 100
        if ret >= _RISE_WARN_PCT:
            rows.append({"code": code, "name": name, "market": mkt,
                         "ret6_pct": round(ret, 1),
                         "over_threshold": ret >= _RISE_THRESHOLD_PCT})
    rows.sort(key=lambda x: -x["ret6_pct"])
    return rows[:40]


def stock_aftermath(code: str, trigger_date: str, horizons=(1, 3, 5, 10)) -> dict:
    """某股「上次觸發處置」前後走勢（純揭露）。trigger_date=處置生效首日(分盤首日)。

    以「處置前最後一天收盤」為基準，算處置後 +1/+3/+5/+10 交易日的漲跌%。
    ret_pct 正=漲、負=跌；放空這段報酬 ≈ -ret_pct。無資料/日期不明回 available=False。
    """
    code = re.sub(r"\.TW[O]?$", "", str(code or "").strip().upper())
    out = {"code": code, "trigger": trigger_date, "available": False, "points": []}
    try:
        dt0 = datetime.strptime(str(trigger_date), "%Y-%m-%d")   # 格式錯（含空）→ 回 available:False 不炸
    except (ValueError, TypeError):
        return out
    start = (dt0 - timedelta(days=45)).strftime("%Y-%m-%d")
    key = f"aftermath:{code}:{trigger_date}"
    hit = _cached(key, ttl=86400)
    if hit is not None:
        return hit
    data, _, ok = _get_json(_FINMIND, {"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start})
    closes = sorted((x["date"], x["close"]) for x in data
                    if x.get("close") not in (None, 0) and x.get("date"))
    if not ok or len(closes) < 2:
        return out
    dates = [d for d, _ in closes]
    ti = next((i for i, d in enumerate(dates) if d >= trigger_date), None)
    if ti is None or ti == 0:
        return out
    prev_i = ti - 1                                   # 處置前最後一天
    base = closes[prev_i][1]
    if not base:
        return out
    points = []
    for h in horizons:
        j = prev_i + h                                # 基準日之後第 h 個交易日
        if j < len(closes):
            points.append({"h": h, "date": closes[j][0],
                           "ret_pct": round((closes[j][1] / base - 1) * 100, 2)})
    out = {"code": code, "trigger": trigger_date, "available": True,
           "prev_date": closes[prev_i][0], "prev_close": base, "points": points}
    return _store(key, out)


def _slope_pct_per_day(closes: list) -> tuple:
    """近 N 日收盤線性回歸斜率 → (%/日, 文字標籤)。用最小平方、不依賴 numpy。"""
    ys = [float(c) for c in closes if c]
    n = len(ys)
    if n < 5:
        return None, "資料不足"
    xs = list(range(n))
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None, "資料不足"
    slope = (n * sxy - sx * sy) / denom
    mean = sy / n
    pct = round(slope / mean * 100, 2) if mean else 0.0
    label = ("陡升" if pct >= 0.5 else "緩升" if pct >= 0.1 else
             "走平" if pct > -0.1 else "緩跌" if pct > -0.5 else "陡跌")
    return pct, label


def stock_intraday(code: str, market: str = "") -> dict:
    """個股當日 1 分 K 走勢 + 現價 + 漲跌幅（對昨收）+ 近20日月斜率（純揭露）。"""
    code = re.sub(r"\.TW[O]?$", "", str(code or "").strip().upper())
    suffix = ".TWO" if str(market).upper() == "TPEX" else ".TW"
    symbol = f"{code}{suffix}"
    out = {"code": code, "available": False, "series": []}
    try:
        from quote_provider import get_quote_provider
        qp = get_quote_provider()
        intr = qp.get_history(symbol, period_days=1, interval="1m")
        daily = qp.get_history(symbol, period_days=40, interval="1d")
    except Exception as e:
        logger.debug("[disposition_radar] intraday 取價失敗 %s: %s", code, e)
        return out
    series, last = [], None
    if intr is not None and not intr.empty:
        idf = intr.dropna(subset=["close"])
        for ts, row in idf.iterrows():
            t = ts.strftime("%H:%M") if hasattr(ts, "strftime") else str(ts)[-8:-3]
            series.append({"t": t, "c": round(float(row["close"]), 2)})
        if series:
            last = series[-1]["c"]
    prev_close = slope_pct = slope_label = None
    if daily is not None and not daily.empty:
        dcloses = [float(x) for x in daily["close"].dropna().tolist()]
        if len(dcloses) >= 2:
            prev_close = dcloses[-2]
            if last is None:
                last = dcloses[-1]
        slope_pct, slope_label = _slope_pct_per_day(dcloses[-20:])
    if last is None:
        return out
    change_pct = round((last / prev_close - 1) * 100, 2) if prev_close else None
    return {"code": code, "available": True, "last": last, "prev_close": prev_close,
            "change_pct": change_pct, "slope_pct_per_day": slope_pct,
            "slope_label": slope_label, "series": series}


# ── 每日 Telegram 推播：新進「再1次就處置」 ─────────────────

def _load_alert_seen() -> dict:
    try:
        if _ALERT_SEEN_PATH.exists():
            return json.loads(_ALERT_SEEN_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_alert_seen(seen: dict):
    try:
        _ALERT_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ALERT_SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False))
    except Exception as e:
        logger.warning("[disposition_radar] alert seen 寫入失敗: %s", e)


def _esc(s) -> str:
    import html
    return html.escape(str(s if s is not None else ""), quote=True)


def run_disposition_alert(now: float | None = None) -> dict:
    """盤後推播「今日新進『再1次就處置』」名單（純觀察、非投資建議）。

    只推 distance≤1 的紅色候選，且 7 天內未推過者（seen store 去重、免洗版）。
    無新進 → 不發送。資料異常(degraded) → 不發送（避免推不完整名單）。
    """
    now = now or time.time()
    radar = compute_radar()
    if radar.get("degraded"):
        logger.warning("[disposition_radar] 資料異常，跳過推播")
        return {"sent": False, "reason": "degraded"}

    reds = [c for c in radar.get("candidates", []) if c.get("distance", 9) <= 1]
    seen = _load_alert_seen()
    cutoff = now - _ALERT_TTL_DAYS * 86400
    seen = {k: v for k, v in seen.items() if v >= cutoff}          # 清過期
    fresh = [c for c in reds if c["code"] not in seen]
    if not fresh:
        for c in reds:                                            # 仍在榜的更新時間戳
            seen[c["code"]] = now
        _save_alert_seen(seen)
        logger.info("[disposition_radar] 無新進紅色候選、不推播")
        return {"sent": False, "reds": len(reds), "fresh": 0}

    lines = ["🚨 <b>處置雷達｜今日新進「再1次就處置」</b>",
             "<i>純觀察追蹤、非投資建議</i>", ""]
    for c in fresh:
        why = "／".join(r["text"] for r in c.get("reasons", [])[:2]) or "接近門檻"
        spy = " 🕵️邊緣徘徊" if c.get("hovering", {}).get("edge_hovering") else ""
        lines.append(f"• <b>{_esc(c['code'])}</b> {_esc(c['name'])}"
                     f"（{_esc(c['market'])}）[{_esc(why)}]{spy}")
    rising_n = radar.get("stats", {}).get("rising", 0)
    if rising_n:
        lines += ["", f"🚀 另有 {rising_n} 檔漲多預警（未列注意、6日漲幅接近門檻）"]
    lines += ["", f"資料日 {_esc(radar.get('as_of'))}｜完整清單見處置雷達頁"]
    msg = "\n".join(lines)

    from notifier import send_telegram
    ok = send_telegram(msg)
    if ok:
        for c in reds:
            seen[c["code"]] = now
        _save_alert_seen(seen)
    logger.info("[disposition_radar] 推播 fresh=%d reds=%d sent=%s", len(fresh), len(reds), ok)
    return {"sent": bool(ok), "reds": len(reds), "fresh": len(fresh),
            "codes": [c["code"] for c in fresh]}
