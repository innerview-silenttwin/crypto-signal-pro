"""處置雷達 預判勝率回測：驗證「距處置 ≤1」訊號的 precision / recall。

問題：歷史上當雷達顯示某檔「再被注意 1 次就處置」時，實際多少比例在幾個交易日內
真的被處置（precision）？反過來，所有處置事件裡有多少在前一交易日就被雷達標到（recall）？

資料源同 backend.disposition_radar（TWSE/TPEx 注意+處置歷史、FinMind 交易日曆）。
用法：pyenv exec python scripts/disposition/backtest_radar_precision.py
2026-07-08 首跑結果（2024-10~2026-07）：precision 3日內 63%、recall 97%。
"""

from __future__ import annotations

import os
import re
import sys
import warnings
from collections import defaultdict

import requests

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
import disposition_radar as dr  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0"}
FM = "https://api.finmindtrade.com/api/v4/data"
# 逐季抓，避免單次 range 過大；(西元, 民國) 各一組
QUARTERS_GREG = [("20250101", "20250630"), ("20250701", "20251231"), ("20260101", "20260707")]
QUARTERS_ROC = [("114/01/01", "114/06/30"), ("114/07/01", "114/12/31"), ("115/01/01", "115/07/07")]
CAL_START = "2024-10-01"


def _twse(url, sd, ed):
    return requests.get(f"{url}?startDate={sd}&endDate={ed}&response=json",
                        headers=UA, timeout=30).json().get("data") or []


def _tpex(url, sd, ed):
    return (requests.get(f"{url}?startDate={sd}&endDate={ed}&response=json", headers=UA,
                         timeout=30, verify=False).json().get("tables") or [{}])[0].get("data") or []


def load_calendar():
    tx = requests.get(FM, params={"dataset": "TaiwanStockPrice", "data_id": "TAIEX",
                                  "start_date": CAL_START}, timeout=25).json().get("data", [])
    return sorted(x["date"] for x in tx)


def load_attention():
    att = defaultdict(lambda: defaultdict(set))
    for sd, ed in QUARTERS_GREG:
        for row in _twse(dr._TWSE_NOTICE, sd, ed):
            c = str(row[1]).strip()
            if re.fullmatch(r"\d{4}", c):
                d = dr.norm_date(str(row[5]))
                if d:
                    att[c][d] |= dr.clause_nums(str(row[4]))
    for sd, ed in QUARTERS_ROC:
        for row in _tpex(dr._TPEX_ATTENTION, sd, ed):
            c = str(row[1]).split("(")[0].strip()
            if re.fullmatch(r"\d{4}", c):
                d = dr.norm_date(str(row[5]))
                if d:
                    att[c][d] |= dr.clause_nums(str(row[4]))
    return att


def load_dispositions():
    disp = defaultdict(list)
    for sd, ed in QUARTERS_GREG:
        for row in _twse(dr._TWSE_PUNISH, sd, ed):
            c = str(row[2]).strip()
            if re.fullmatch(r"\d{4}", c):
                p = dr.parse_period(str(row[6]))
                if p:
                    disp[c].append(p)
    for sd, ed in QUARTERS_ROC:
        for row in _tpex(dr._TPEX_DISPOSAL, sd, ed):
            c = str(row[2]).split("(")[0].strip()
            if re.fullmatch(r"\d{4}", c):
                p = dr.parse_period(str(row[5]))
                if p:
                    disp[c].append(p)
    return disp


def main():
    CAL = load_calendar()
    cal_idx = {d: i for i, d in enumerate(CAL)}
    att = load_attention()
    disp = load_dispositions()
    disp_starts = {c: sorted({s for s, _ in pl}) for c, pl in disp.items()}

    def plus_bdays(d, n):
        i = cal_idx.get(d)
        return CAL[i + n] if i is not None and i + n < len(CAL) else None

    def last_end_before(code, d):
        ends = [e for s, e in disp.get(code, []) if e < d]
        return max(ends) if ends else None

    print(f"注意股 {len(att)}、處置股 {len(disp)}、交易日 {len(CAL)} ({CAL[0]}~{CAL[-1]})")

    # 每個處置週期第一次 distance<=1 記為一次訊號
    signals = []
    for code, byd in att.items():
        fired_reset = None
        for d in sorted(byd):
            reset = last_end_before(code, d)
            if fired_reset is not None and reset != fired_reset:
                fired_reset = None
            if fired_reset is not None:
                continue
            cal_upto = [x for x in CAL if x <= d]
            res = dr.distance_to_disposition({dd: set(cs) for dd, cs in byd.items()},
                                             cal_upto, reset_after=reset)
            if res["distance"] is not None and res["distance"] <= 1:
                signals.append((code, d))
                fired_reset = reset

    print(f"\n[precision] 訊號 {len(signals)} 筆")
    for K in (1, 2, 3, 5):
        hit = sum(1 for code, d in signals
                  if (dl := plus_bdays(d, K)) and any(d < s <= dl for s in disp_starts.get(code, [])))
        print(f"  {K} 交易日內真的進處置：{hit} ({100 * hit / max(1, len(signals)):.0f}%)")

    disp_events = [(c, s) for c, ss in disp_starts.items() for s in ss]
    flagged = evaluable = 0
    for code, s in disp_events:
        i = cal_idx.get(s)
        if not i:
            continue
        prev = CAL[i - 1]
        byd = att.get(code)
        if not byd:
            continue
        evaluable += 1
        res = dr.distance_to_disposition({dd: set(cs) for dd, cs in byd.items()},
                                         [x for x in CAL if x <= prev],
                                         reset_after=last_end_before(code, prev))
        if res["distance"] is not None and res["distance"] <= 1:
            flagged += 1
    print(f"\n[recall] 處置事件 {len(disp_events)}、可評估 {evaluable}；"
          f"前一交易日已被標≤1：{flagged} ({100 * flagged / max(1, evaluable):.0f}%)")


if __name__ == "__main__":
    main()
