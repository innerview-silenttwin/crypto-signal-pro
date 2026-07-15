"""disposition_radar 單元測試（不打網路）。重點：距處置計數 + 計數重置。"""

import os
import sys

import pytest

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import disposition_radar as dr


@pytest.fixture(autouse=True)
def _no_exdiv_network(monkeypatch):
    """stock_intraday 會查除息參考價（FinMind）——測試預設擋掉；除息情境測試自行覆寫。
    同時清 dr._cache 避免 exdiv 短 TTL 快取跨測試污染。"""
    dr._cache.clear()
    monkeypatch.setattr(dr, "get_ex_dividend_ref", lambda *a, **k: None)


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
    assert dr.norm_date("2026-07-07") == "2026-07-07"   # ISO '-' 分隔也要吃
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


def test_reasons_carry_reliability():
    """A/B/C 標 reliable=True、D（30日）標 False，供前端顯示準/參考。"""
    cal = _cal([f"2026-06-{d:02d}" for d in range(1, 28)])
    # 連 2 天第一款（A，準）
    byd = {cal[-2]: {1}, cal[-1]: {1}}
    r = dr.distance_to_disposition(byd, cal)
    a = next(x for x in r["reasons"] if x["rule"] == "A")
    assert a["reliable"] is True
    # 純 30 日內次數（D，參考）：非連續、只沾 in30
    byd = {cal[i]: {4} for i in (0, 3, 6, 9, 12, 15, 18, 21, 24)}  # 9 次、間隔
    r = dr.distance_to_disposition(byd, cal)
    d = next((x for x in r["reasons"] if x["rule"] == "D"), None)
    assert d is not None and d["reliable"] is False


def _radar_stubs(monkeypatch, byd, name, market, periods, att_ok=True, disp_ok=True,
                 cal_ok=True, cal=None):
    cal = cal or _cal([f"2026-07-{d:02d}" for d in (1, 2, 3, 6, 7)])
    monkeypatch.setattr(dr, "_trading_calendar", lambda n=40: (cal, cal_ok))
    monkeypatch.setattr(dr, "_fetch_attention_history", lambda bdays: (byd, name, market, {}, att_ok))
    # 由 periods 合成 detail（level/measure 空即可）供處置中明細
    detail = {c: [{"start": s, "end": e, "level": "處置中", "measure": ""} for s, e in pl]
              for c, pl in periods.items()}
    monkeypatch.setattr(dr, "_fetch_disposition_periods", lambda bdays: (periods, detail, disp_ok))
    dr._cache.clear()


def test_compute_radar_excludes_active_disposition(monkeypatch):
    """已在處置中的股票不列入 candidates、放 in_disposition。"""
    _radar_stubs(monkeypatch,
                 {"1111": {"2026-07-06": {1}, "2026-07-07": {1}},   # 連 2 天第一款 → 候選
                  "2222": {"2026-07-06": {1}, "2026-07-07": {1}}},  # 在處置中 → 排除
                 {"1111": "甲股", "2222": "乙股"},
                 {"1111": "TWSE", "2222": "TPEx"},
                 {"2222": [("2026-07-01", "2026-07-14")]})          # 涵蓋 today
    out = dr.compute_radar(today="2026-07-07")
    assert [c["code"] for c in out["candidates"]] == ["1111"]
    assert [d["code"] for d in out["in_disposition"]] == ["2222"]
    assert out["candidates"][0]["distance"] == 1
    assert out["stats"]["red"] == 1
    assert out["degraded"] is False


def test_compute_radar_future_disposition_not_vanish(monkeypatch):
    """盤後剛公告、明日才生效（start>today）的處置：該股要進 in_disposition、
    不可因未來 end 把計數清空而從候選『憑空消失』（review 抓到的 bug 回歸）。"""
    _radar_stubs(monkeypatch,
                 {"3333": {"2026-07-06": {1}, "2026-07-07": {1}}},
                 {"3333": "丙股"}, {"3333": "TWSE"},
                 {"3333": [("2026-07-08", "2026-07-21")]})          # start>today(07-07)
    out = dr.compute_radar(today="2026-07-07")
    assert [d["code"] for d in out["in_disposition"]] == ["3333"]   # 有被surface
    assert out["in_disposition"][0]["pending"] is True              # 已公告未生效
    assert [c["code"] for c in out["candidates"]] == []             # 不在候選、但沒消失


def test_compute_radar_in_disposition_detail(monkeypatch):
    """處置中明細：帶等級/措施原文/出關日/可能再處置（加重 or 近期曾處置）。"""
    cal = _cal([f"2026-07-{d:02d}" for d in (1, 2, 3, 6, 7)])
    monkeypatch.setattr(dr, "_trading_calendar", lambda n=40: (cal, True))
    monkeypatch.setattr(dr, "_fetch_attention_history", lambda bdays: (
        {"2222": {"2026-07-06": {1}}}, {"2222": "乙股"}, {"2222": "TWSE"},
        {"2222": {"date": "2026-07-06", "text": "累積漲幅達40%(第一款)"}}, True))
    monkeypatch.setattr(dr, "_fetch_disposition_periods", lambda bdays: (
        {"2222": [("2026-06-10", "2026-06-23"), ("2026-07-01", "2026-07-14")]},
        {"2222": [{"start": "2026-06-10", "end": "2026-06-23", "level": "第一次(約5分盤)", "measure": "五分鐘"},
                  {"start": "2026-07-01", "end": "2026-07-14", "level": "加重(約20分盤)", "measure": "二十分鐘全額預收"}]},
        True))
    dr._cache.clear()
    out = dr.compute_radar(today="2026-07-07")
    d = out["in_disposition"][0]
    assert d["code"] == "2222" and d["end"] == "2026-07-14"     # 取涵蓋 today 的期
    assert d["level"] == "加重(約20分盤)" and "全額預收" in d["measure"]
    assert d["re_risk"] is True                                  # 加重 + 近期曾處置
    assert d["latest_notice"]["text"].startswith("累積漲幅")


def test_in_disposition_prefers_running_over_future(monkeypatch):
    """同時有現行期(涵蓋today)與已公告未來期 → 顯示現行期（非即將生效、出關日為現行期）。"""
    cal = _cal([f"2026-07-{d:02d}" for d in (1, 2, 3, 6, 7)])
    monkeypatch.setattr(dr, "_trading_calendar", lambda n=40: (cal, True))
    monkeypatch.setattr(dr, "_fetch_attention_history", lambda bdays: (
        {"5555": {"2026-07-06": {1}}}, {"5555": "戊股"}, {"5555": "TWSE"}, {}, True))
    monkeypatch.setattr(dr, "_fetch_disposition_periods", lambda bdays: (
        {"5555": [("2026-07-01", "2026-07-14"), ("2026-07-20", "2026-08-02")]},
        {"5555": [{"start": "2026-07-01", "end": "2026-07-14", "level": "第一次(約5分盤)", "measure": "五分鐘"},
                  {"start": "2026-07-20", "end": "2026-08-02", "level": "加重(約20分盤)", "measure": "二十分鐘"}]},
        True))
    dr._cache.clear()
    d = dr.compute_radar(today="2026-07-07")["in_disposition"][0]
    assert d["end"] == "2026-07-14"       # 現行期出關日，非未來期 08-02
    assert d["pending"] is False          # 進行中、非即將生效


def test_compute_radar_past_disposition_resets_but_keeps_fresh(monkeypatch):
    """已結束的處置（end<today）重置計數，但處置後新累積的注意仍算。"""
    cal = _cal([f"2026-07-{d:02d}" for d in (1, 2, 3, 6, 7)])
    _radar_stubs(monkeypatch,
                 {"4444": {"2026-07-06": {1}, "2026-07-07": {1}}},  # 處置(6/30結束)後連2天
                 {"4444": "丁股"}, {"4444": "TWSE"},
                 {"4444": [("2026-06-16", "2026-06-30")]}, cal=cal)  # end<today
    out = dr.compute_radar(today="2026-07-07")
    assert [c["code"] for c in out["candidates"]] == ["4444"]
    assert out["candidates"][0]["distance"] == 1                    # 連2天第一款


def test_compute_radar_degraded_when_fetch_fails(monkeypatch):
    """注意抓取失敗 → degraded=True 且不快取（下次重試，不 silent stale）。"""
    _radar_stubs(monkeypatch, {}, {}, {}, {}, att_ok=False)
    out = dr.compute_radar(today="2026-07-07")
    assert out["degraded"] is True
    assert out["sources"]["attention"] is False
    assert dr._cache == {}                                          # 沒被快取


def test_compute_radar_degraded_when_calendar_fails(monkeypatch):
    """TAIEX 日曆抓取失敗 → degraded=True（否則只剩注意日期的稀疏日曆會虛構連續天數）。"""
    _radar_stubs(monkeypatch,
                 {"1111": {"2026-07-06": {1}, "2026-07-07": {1}}},
                 {"1111": "甲股"}, {"1111": "TWSE"}, {}, cal_ok=False)
    out = dr.compute_radar(today="2026-07-07")
    assert out["degraded"] is True
    assert out["sources"]["calendar"] is False
    assert dr._cache == {}


def test_hovering_stats_edge_hoverer():
    """反覆『連2天第一款→斷』無限逼近卻不觸發 → edge_hovering=True（貓膩型態）。"""
    cal = _cal([f"2026-07-{d:02d}" for d in range(1, 9)])   # 8 交易日
    # 第一款注意落在 index 0,1 / 3,4 / 6,7（每對的第二天 distance=1）
    byd = {cal[i]: {1} for i in (0, 1, 3, 4, 6, 7)}
    h = dr._hovering_stats(byd, [], cal, window_days=30)
    # 連2天第一款的第二天(cal[1]/cal[4]) + 後段 10 日內次數累積也讓 cal[6]/cal[7] 站上再1次 → 4 天
    assert h["near_miss_days"] == 4
    assert h["triggers"] == 0
    assert h["edge_hovering"] is True


def test_hovering_stats_serial_offender_not_flagged():
    """真的一直被處置（triggers 多）→ 不算邊緣徘徊。"""
    cal = _cal([f"2026-07-{d:02d}" for d in range(1, 9)])
    byd = {cal[i]: {1} for i in (0, 1, 3, 4, 6, 7)}
    periods = [("2026-07-02", "2026-07-02"), ("2026-07-05", "2026-07-05")]  # 2 次觸發
    h = dr._hovering_stats(byd, periods, cal, window_days=30)
    assert h["triggers"] == 2
    assert h["edge_hovering"] is False


def test_compute_radar_candidate_carries_hovering(monkeypatch):
    _radar_stubs(monkeypatch,
                 {"1111": {"2026-07-06": {1}, "2026-07-07": {1}}},
                 {"1111": "甲股"}, {"1111": "TWSE"}, {})
    out = dr.compute_radar(today="2026-07-07")
    assert "hovering" in out["candidates"][0]
    assert "near_miss_days" in out["candidates"][0]["hovering"]


def test_rising_radar_flags_and_excludes(monkeypatch):
    """漲多預警：≥25% 入榜、≥32% 標門檻、已在 exclude(已列注意/處置)者剔除。"""
    cal = _cal([f"2026-07-{d:02d}" for d in range(1, 9)])   # ≥6 天
    base = {"1111": ("甲", 100.0, "TWSE"), "2222": ("乙", 100.0, "TWSE"),
            "3333": ("丙", 100.0, "TPEx"), "4444": ("丁", 100.0, "TWSE")}
    latest = {"1111": ("甲", 140.0, "TWSE"),   # +40% → 入榜且過門檻
              "2222": ("乙", 128.0, "TWSE"),   # +28% → 入榜未過門檻
              "3333": ("丙", 110.0, "TPEx"),   # +10% → 不入榜
              "4444": ("丁", 150.0, "TWSE")}   # +50% 但在 exclude → 剔除
    calls = {"n": 0}
    def fake(date):
        calls["n"] += 1
        return (base, True) if date == cal[-6] else (latest, True)
    monkeypatch.setattr(dr, "_all_market_closes", fake)
    out = dr.rising_radar(cal, exclude={"4444"})
    codes = [r["code"] for r in out]
    assert codes == ["1111", "2222"]            # 依漲幅排序、排除 4444、濾掉 3333
    assert out[0]["over_threshold"] is True and out[1]["over_threshold"] is False


def test_rising_radar_empty_on_fetch_fail(monkeypatch):
    monkeypatch.setattr(dr, "_all_market_closes", lambda date: ({}, False))
    assert dr.rising_radar(_cal([f"2026-07-{d:02d}" for d in range(1, 9)]), set()) == []


def test_stock_aftermath_trajectory(monkeypatch):
    """觸發後走勢：以處置前最後一天收盤為基準算 +1/+3/+5/+10 漲跌%。"""
    pairs = [("2026-05-20", 100), ("2026-05-21", 100), ("2026-05-22", 90), ("2026-05-23", 100),
             ("2026-05-24", 108), ("2026-05-25", 100), ("2026-05-26", 50), ("2026-05-27", 100),
             ("2026-05-28", 100), ("2026-05-29", 100), ("2026-05-30", 100), ("2026-05-31", 121)]
    monkeypatch.setattr(dr, "_get_json",
                        lambda url, params: ([{"date": d, "close": c} for d, c in pairs], [], True))
    dr._cache.clear()
    a = dr.stock_aftermath("6182", "2026-05-22")
    assert a["available"] is True
    assert a["prev_date"] == "2026-05-21" and a["prev_close"] == 100
    pts = {p["h"]: p["ret_pct"] for p in a["points"]}
    assert pts == {1: -10.0, 3: 8.0, 5: -50.0, 10: 21.0}


def test_stock_aftermath_unavailable(monkeypatch):
    monkeypatch.setattr(dr, "_get_json", lambda url, params: ([], [], False))
    dr._cache.clear()
    assert dr.stock_aftermath("6182", "2026-05-22")["available"] is False
    assert dr.stock_aftermath("6182", "")["available"] is False


def test_stock_aftermath_malformed_trigger_no_crash():
    """格式錯的 trigger（斜線/亂碼/None）不可 strptime 崩潰 → 回 available:False。"""
    for bad in ["2026/05/22", "20260522", "garbage", None]:
        assert dr.stock_aftermath("6182", bad)["available"] is False


def test_stock_intraday_high_low_volume_session(monkeypatch):
    """intraday：帶最高/最低/每根量；盤中時段過濾（濾掉 09:00 前與 13:30 後怪點）。"""
    import pandas as pd

    class _QP:
        def get_history(self, symbol, period_days=1, interval="1m"):
            if interval == "1m":
                idx = pd.to_datetime(["2026-07-13 08:45", "2026-07-13 09:01",
                                      "2026-07-13 13:24", "2026-07-13 17:01"]).tz_localize("Asia/Taipei")
                return pd.DataFrame({"close": [95.0, 100.0, 104.0, 999.0],
                                     "high": [95.5, 101.0, 106.5, 999.0],
                                     "low": [94.0, 99.5, 103.0, 999.0],
                                     "volume": [1000, 2000, 3000, 500]}, index=idx)
            idx = pd.to_datetime(["2026-07-10", "2026-07-13"])
            return pd.DataFrame({"close": [98.0, 104.0]}, index=idx)

    import quote_provider
    monkeypatch.setattr(quote_provider, "get_quote_provider", lambda: _QP())
    d = dr.stock_intraday("1234", "TWSE")
    assert d["available"] is True
    assert [p["t"] for p in d["series"]] == ["09:01", "13:24"]   # 08:45 / 17:01 被濾
    assert d["series"][0]["v"] == 2                               # yfinance 股→張(÷1000)
    assert d["last"] == 104.0
    # 高低價只從「盤中時段」K 棒算：盤外怪點（08:45 的 95 / 17:01 的 999）不可污染
    assert d["day_high"] == 106.5
    assert d["day_low"] == 99.5
    assert d["change_pct"] == 6.12                                # 104 vs 昨收 98


def test_stock_intraday_volume_unit_by_attrs(monkeypatch):
    """成交量單位依 df.attrs 標記正規化成張：lots 直通、shares ÷1000。
    關鍵情境：sinopac provider 內部 fallback yfinance 時，類名仍是 Sinopac 但資料是股——
    必須看 attrs 不能看類名（review 抓到）。"""
    import pandas as pd

    def _mk_provider(unit, vol):
        class SinopacQuoteProviderFake:        # 類名故意叫 Sinopac（模擬 fallback 情境）
            def get_history(self, symbol, period_days=1, interval="1m"):
                if interval == "1m":
                    idx = pd.to_datetime(["2026-07-13 09:01"]).tz_localize("Asia/Taipei")
                    df = pd.DataFrame({"close": [100.0], "high": [101.0], "low": [99.0],
                                       "volume": [vol]}, index=idx)
                    if unit:
                        df.attrs["volume_unit"] = unit
                    return df
                idx = pd.to_datetime(["2026-07-10", "2026-07-13"])
                return pd.DataFrame({"close": [98.0, 100.0]}, index=idx)
        return SinopacQuoteProviderFake()

    import quote_provider
    # sinopac 原生 kbars（attrs=lots、132 張）→ 直通
    monkeypatch.setattr(quote_provider, "get_quote_provider", lambda: _mk_provider("lots", 132))
    assert dr.stock_intraday("1234", "TWSE")["series"][0]["v"] == 132
    # sinopac 類名但 fallback yfinance（attrs=shares、132000 股）→ ÷1000 = 132 張
    monkeypatch.setattr(quote_provider, "get_quote_provider", lambda: _mk_provider("shares", 132000))
    assert dr.stock_intraday("1234", "TWSE")["series"][0]["v"] == 132
    # 未標記 → 退回類名推斷（Sinopac 開頭 → 當張）
    monkeypatch.setattr(quote_provider, "get_quote_provider", lambda: _mk_provider(None, 55))
    assert dr.stock_intraday("1234", "TWSE")["series"][0]["v"] == 55


def test_stock_intraday_keeps_only_last_day(monkeypatch):
    """sinopac 1m 會回多天 K 棒 → 只留最後一個交易日（mini 實測 1567 根＝6 天串在一起）。"""
    import pandas as pd

    class _QP:
        def get_history(self, symbol, period_days=1, interval="1m"):
            if interval == "1m":
                idx = pd.to_datetime(["2026-07-10 09:01", "2026-07-10 13:00",   # 前一交易日
                                      "2026-07-13 09:01", "2026-07-13 13:00"]).tz_localize("Asia/Taipei")
                df = pd.DataFrame({"close": [18.6, 19.0, 23.8, 24.05],
                                   "high": [19.0, 19.2, 24.0, 24.3],
                                   "low": [18.5, 18.9, 23.5, 23.9],
                                   "volume": [100, 100, 200, 300]}, index=idx)
                df.attrs["volume_unit"] = "lots"
                return df
            idx = pd.to_datetime(["2026-07-10", "2026-07-13"])
            return pd.DataFrame({"close": [19.0, 24.05]}, index=idx)

    import quote_provider
    monkeypatch.setattr(quote_provider, "get_quote_provider", lambda: _QP())
    d = dr.stock_intraday("2332", "TWSE")
    assert len(d["series"]) == 2                       # 只剩 07-13 兩根
    assert d["day_low"] == 23.5                        # 前一日的 18.5/18.6 不可污染當日低
    assert d["last"] == 24.05


def test_stock_intraday_bad_tick_high_low_clamped(monkeypatch):
    """1m high/low 壞 tick（超出昨收±11%）→ 改用收盤極值（mini 實測 sinopac low 18.6/昨收 25.95）。"""
    import pandas as pd

    class _QP:
        def get_history(self, symbol, period_days=1, interval="1m"):
            if interval == "1m":
                idx = pd.to_datetime(["2026-07-13 09:01", "2026-07-13 10:00"]).tz_localize("Asia/Taipei")
                df = pd.DataFrame({"close": [24.0, 24.05], "high": [26.45, 24.5],
                                   "low": [18.6, 23.9],           # 18.6 = 壞 tick（跌停下限 23.36）
                                   "volume": [1000, 2000]}, index=idx)
                df.attrs["volume_unit"] = "lots"
                return df
            idx = pd.to_datetime(["2026-07-10", "2026-07-13"])
            return pd.DataFrame({"close": [25.95, 24.05]}, index=idx)

    import quote_provider
    monkeypatch.setattr(quote_provider, "get_quote_provider", lambda: _QP())
    d = dr.stock_intraday("2332", "TWSE")
    assert d["day_low"] == 24.0                             # 壞 tick 18.6 → 改用收盤極值
    assert d["day_high"] == 26.45                           # 26.45 < 25.95*1.11=28.8 合法保留


def test_stock_intraday_ex_dividend_ref_price(monkeypatch):
    """除權息生效日：平盤=除息參考價，漲跌幅/壞tick 基準隨之（3034 情境）。"""
    import pandas as pd

    class _QP:
        def get_history(self, symbol, period_days=1, interval="1m"):
            if interval == "1m":
                idx = pd.to_datetime(["2026-07-13 09:01"]).tz_localize("Asia/Taipei")
                df = pd.DataFrame({"close": [467.5], "high": [470.0], "low": [465.0],
                                   "volume": [100]}, index=idx)
                df.attrs["volume_unit"] = "lots"
                return df
            idx = pd.to_datetime(["2026-07-09", "2026-07-13"])
            return pd.DataFrame({"close": [542.0, 467.5]}, index=idx)

    import quote_provider
    monkeypatch.setattr(quote_provider, "get_quote_provider", lambda: _QP())
    monkeypatch.setattr(dr, "get_ex_dividend_ref",
                        lambda code, pds, cds: 519.0 if (code, pds, cds) == ("3034", "2026-07-09", "2026-07-13") else None)
    d = dr.stock_intraday("3034", "TWSE")
    assert d["ref_price"] == 519.0 and d["ref_kind"] == "除權息參考價"
    assert d["change_pct"] == round((467.5 / 519.0 - 1) * 100, 2)   # -9.92（對參考價，非對 542 的 -13.7）
    assert d["prev_close"] == 542.0                                  # 昨收照樣回（顯示用）


def test_stock_intraday_normal_day_ref_is_prev_close(monkeypatch):
    import pandas as pd

    class _QP:
        def get_history(self, symbol, period_days=1, interval="1m"):
            if interval == "1m":
                idx = pd.to_datetime(["2026-07-13 09:01"]).tz_localize("Asia/Taipei")
                df = pd.DataFrame({"close": [104.0], "high": [105.0], "low": [103.0],
                                   "volume": [10]}, index=idx)
                df.attrs["volume_unit"] = "lots"
                return df
            idx = pd.to_datetime(["2026-07-10", "2026-07-13"])
            return pd.DataFrame({"close": [98.0, 104.0]}, index=idx)

    import quote_provider
    monkeypatch.setattr(quote_provider, "get_quote_provider", lambda: _QP())
    d = dr.stock_intraday("1234", "TWSE")
    assert d["ref_price"] == 98.0 and d["ref_kind"] == "昨收"
    assert d["change_pct"] == 6.12


def test_providers_tag_volume_unit():
    """兩個 provider 的 df 都要帶 volume_unit 標記（正規化依據）。"""
    import pandas as pd
    from quote_provider.sinopac_provider import SinopacQuoteProvider

    class _KB:
        ts = [int(pd.Timestamp("2026-07-13 09:01:00").value)]
        Open = [100.0]; High = [101.0]; Low = [99.0]; Close = [100.5]; Volume = [132]

    df = SinopacQuoteProvider._kbars_to_df(_KB())
    assert df.attrs.get("volume_unit") == "lots"


def test_run_disposition_alert_dedup_and_degraded(monkeypatch, tmp_path):
    """推播：新進紅色候選才發、7天內去重、degraded 不發。"""
    import notifier
    monkeypatch.setattr(dr, "_ALERT_SEEN_PATH", tmp_path / "alert_seen.json")
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda m: (sent.append(m), True)[1])

    radar = {"degraded": False, "as_of": "2026-07-08", "stats": {"rising": 2},
             "candidates": [
                 {"code": "6525", "name": "捷敏-KY", "market": "TWSE", "distance": 1,
                  "reasons": [{"text": "連2天第一款"}], "hovering": {"edge_hovering": False}},
                 {"code": "2466", "name": "冠西電", "market": "TWSE", "distance": 0,
                  "reasons": [{"text": "連3天第一款"}], "hovering": {"edge_hovering": True}},
                 {"code": "9999", "name": "遠的", "market": "TWSE", "distance": 3,  # 非紅、不推
                  "reasons": [{"text": "10日內3次"}], "hovering": {"edge_hovering": False}}]}
    monkeypatch.setattr(dr, "compute_radar", lambda *a, **k: radar)

    r1 = dr.run_disposition_alert(now=1000.0)
    assert r1["sent"] is True and r1["fresh"] == 2          # 只推 2 檔紅色（distance≤1）
    assert set(r1["codes"]) == {"6525", "2466"}
    assert "🕵️邊緣徘徊" in sent[0] and "9999" not in sent[0]

    # 同一批隔天再跑 → 已在 seen、不重推
    r2 = dr.run_disposition_alert(now=1000.0 + 86400)
    assert r2["sent"] is False and r2["fresh"] == 0
    assert len(sent) == 1

    # degraded → 不發
    monkeypatch.setattr(dr, "compute_radar", lambda *a, **k: {"degraded": True})
    assert dr.run_disposition_alert()["sent"] is False


def test_schema_ok_detects_missing_code_column():
    """有資料卻定位不到證券代號欄（表頭改版）→ _schema_ok False（供標 degraded）。"""
    good = ["編號", "證券代號", "證券名稱", "日期"]
    bad = ["編號", "代碼X", "名字X", "時間X"]      # 找不到「證券代號/代號」
    assert dr._schema_ok([[1, "2330", "台積電", "115/07/07"]], good) is True
    assert dr._schema_ok([[1, "2330", "台積電", "115/07/07"]], bad) is False
    assert dr._schema_ok([], bad) is True          # 真的沒資料不算異常


def test_parse_notice_rows_by_field_header():
    """欄位用 fields 表頭定位，欄序改變也不錯位；短列/非普通股略過。"""
    fields = ["編號", "證券代號", "證券名稱", "累計次數", "注意交易資訊", "日期", "收盤價"]
    data = [
        [1, "2330", "台積電", 1, "最近六個營業日累積漲幅達40%（第一款）", "115/07/07", "1000"],
        [2, "050328", "某購01", 1, "（第一款）", "115/07/07", "20"],     # 6碼權證→略過
        [3, "1234"],                                                     # 短列→略過
    ]
    out = dr.parse_notice_rows(data, fields, "TWSE")
    assert out == [("2330", "台積電", "2026-07-07", {1}, "TWSE",
                    "最近六個營業日累積漲幅達40%（第一款）")]      # 第6元素=原文(含數字)

    # 欄序被打亂（代號移到後面）仍正確；<br> 清成換行
    fields2 = ["日期", "證券名稱", "證券代號", "注意交易資訊"]
    data2 = [["115/07/07", "聯電", "2303", "累積漲幅（第一款）<br>週轉率（第四款）"]]
    out2 = dr.parse_notice_rows(data2, fields2, "TWSE")
    assert out2[0][:5] == ("2303", "聯電", "2026-07-07", {1, 4}, "TWSE")
    assert out2[0][5] == "累積漲幅（第一款）\n週轉率（第四款）"


def test_parse_disposal_rows_by_field_header():
    """處置期間欄位『起迄』vs『起訖』用字不同也要抓到。"""
    fields_tw = ["編號", "公布日期", "證券代號", "證券名稱", "累計", "處置起迄時間", "處置內容"]
    data_tw = [[1, "115/07/06", "6488", "環球晶", 2, "115/07/07～115/07/20", "約每二十分鐘撮合一次"]]
    row = dr.parse_disposal_rows(data_tw, fields_tw)[0]
    assert row[0] == "6488" and row[1] == "2026-07-07" and row[2] == "2026-07-20"
    assert row[3] == "加重(約20分盤)"                       # 由「二十分鐘」判等級
    fields_tp = ["編號", "公布日期", "證券代號", "證券名稱", "累計", "處置起訖時間", "處置內容"]  # 訖
    data_tp = [[1, "115/07/06", "6182", "合晶", 1, "115/07/07~115/07/20", "約每五分鐘撮合一次"]]
    row2 = dr.parse_disposal_rows(data_tp, fields_tp)[0]
    assert row2[0] == "6182" and row2[3] == "第一次(約5分盤)"
