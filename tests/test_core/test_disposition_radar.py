"""disposition_radar 單元測試（不打網路）。重點：距處置計數 + 計數重置。"""

import os
import sys

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import disposition_radar as dr


def _cal(dates):
    return sorted(dates)


def test_clause_nums_parses_multiple():
    assert dr.clause_nums("最近六個營業日累積漲幅達43%（第一款）") == {1}
    assert dr.clause_nums("累積漲幅（第一款）且週轉率（第四款）") == {1, 4}
    assert dr.clause_nums("借券賣出（第十二款）") == {12}
    assert dr.clause_nums("") == set()


def test_norm_date_roc_and_greg():
    assert dr.norm_date("115/07/07") == "2026-07-07"
    assert dr.norm_date("115.07.07") == "2026-07-07"
    assert dr.norm_date("2026/7/7") == "2026-07-07"
    assert dr.norm_date("") is None
    assert dr.norm_date("garbage") is None


def test_parse_period():
    assert dr.parse_period("115/07/07~115/07/20") == ("2026-07-07", "2026-07-20")
    assert dr.parse_period("民國115年7月7日") is None       # 只有一個日期
    assert dr.parse_period("") is None


def test_distance_consecutive_first_clause():
    """連 2 天第一款 → 再 1 次就處置（notetrans「連續二次」情境）。"""
    cal = _cal(["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-07"])
    byd = {"2026-07-04": {1}, "2026-07-07": {1}}   # 最後兩個交易日連續第一款
    r = dr.distance_to_disposition(byd, cal)
    assert r["counts"]["consec_first"] == 2
    assert r["distance"] == 1        # 3 - 2
    assert r["tier"] == "red"


def test_distance_six_in_ten():
    """最近 10 交易日內 5 次 1-8 款 → 再 1 次（規則 C）。
    用非連續日期，避免同時觸發「連續 N 天」規則 B（否則距離會由 B 主導）。"""
    cal = _cal([f"2026-06-{d:02d}" for d in range(15, 31)])  # >10 個交易日
    hit = [cal[-1], cal[-3], cal[-5], cal[-7], cal[-9]]      # 5 次、間隔開
    byd = {d: {4} for d in hit}
    r = dr.distance_to_disposition(byd, cal)
    assert r["counts"]["in10"] == 5
    assert r["counts"]["consec_18"] == 1     # 沒連續、B 不主導
    assert r["distance"] == 1        # 6 - 5（規則 C）


def test_distance_tiers():
    cal = _cal([f"2026-06-{d:02d}" for d in range(1, 28)])
    # 10 日內 4 次（間隔開，避免觸發連續規則）→ 距離 2 → orange
    byd = {d: {4} for d in [cal[-2], cal[-4], cal[-6], cal[-8]]}
    assert dr.distance_to_disposition(byd, cal)["tier"] == "orange"
    # 10 日內 3 次 → 距離 3 → yellow
    byd = {d: {4} for d in [cal[-2], cal[-4], cal[-6]]}
    assert dr.distance_to_disposition(byd, cal)["tier"] == "yellow"


def test_distance_none_when_no_attention():
    cal = _cal(["2026-07-01", "2026-07-02"])
    assert dr.distance_to_disposition({}, cal)["distance"] is None


def test_reset_after_zeroes_old_counts():
    """計數重置：處置結束前的注意不算，避免舊次數殘留誤報（6116 彩晶情境）。"""
    cal = _cal([f"2026-06-{d:02d}" for d in range(1, 28)])
    # 前 12 天各一次注意（處置前殘留），處置 6/15 結束，之後完全沒注意
    byd = {d: {4} for d in cal[:12]}
    r = dr.distance_to_disposition(byd, cal, reset_after="2026-06-15")
    assert r["distance"] is None      # 重置後無任何注意 → 不列入
    # 無重置時會誤報成逼近
    r2 = dr.distance_to_disposition(byd, cal)
    assert r2["counts"]["in30"] == 12


def test_reset_keeps_post_disposition_attention():
    cal = _cal([f"2026-06-{d:02d}" for d in range(1, 28)])
    byd = {d: {1} for d in cal[-2:]}   # 處置後又連 2 天第一款
    r = dr.distance_to_disposition(byd, cal, reset_after="2026-06-20")
    assert r["counts"]["consec_first"] == 2
    assert r["distance"] == 1


def test_distance_clamped_at_zero():
    """已達門檻（連 3 天第一款）→ 距離夾 0，不出現負值。"""
    cal = _cal(["2026-07-01", "2026-07-02", "2026-07-03"])
    byd = {d: {1} for d in cal}
    r = dr.distance_to_disposition(byd, cal)
    assert r["distance"] == 0
    assert r["tier"] == "red"


def test_compute_radar_excludes_active_disposition(monkeypatch):
    """已在處置中的股票不列入 candidates、放 in_disposition。"""
    cal = _cal([f"2026-07-{d:02d}" for d in (1, 2, 3, 6, 7)])
    monkeypatch.setattr(dr, "_trading_calendar", lambda n=40: cal)
    monkeypatch.setattr(dr, "_fetch_attention_history", lambda bdays: (
        {"1111": {"2026-07-06": {1}, "2026-07-07": {1}},   # 連 2 天第一款 → 候選
         "2222": {"2026-07-06": {1}, "2026-07-07": {1}}},  # 但在處置中 → 排除
        {"1111": "甲股", "2222": "乙股"},
        {"1111": "TWSE", "2222": "TPEx"}))
    monkeypatch.setattr(dr, "_fetch_disposition_periods", lambda bdays: {
        "2222": [("2026-07-01", "2026-07-14")]})            # 涵蓋 today
    dr._cache.clear()
    out = dr.compute_radar(today="2026-07-07")
    codes = [c["code"] for c in out["candidates"]]
    assert codes == ["1111"]
    assert out["in_disposition"] == ["2222"]
    assert out["candidates"][0]["distance"] == 1
    assert out["stats"]["red"] == 1
