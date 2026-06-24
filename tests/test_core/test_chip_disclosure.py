"""chip_disclosure 解析/淨計算/單位換算單元測試（monkeypatch 掉網路）。"""

import os
import sys

import pytest

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import chip_disclosure as cd


@pytest.fixture(autouse=True)
def _clear_cache():
    cd._cache.clear()
    yield
    cd._cache.clear()


def test_net():
    assert cd._net(100, 30) == 70.0
    assert cd._net(None, None) == 0.0
    assert cd._net("bad", 5) == 0.0          # 容錯
    assert cd._net(10, 25) == -15.0


def test_futures_oi_net_is_long_minus_short(monkeypatch):
    rows = [
        # 外資：多單 OI 100、空單 OI 800 → 淨 -700
        {"date": "2026-06-23", "institutional_investors": "外資",
         "long_open_interest_balance_volume": 100, "short_open_interest_balance_volume": 800},
        {"date": "2026-06-23", "institutional_investors": "投信",
         "long_open_interest_balance_volume": 600, "short_open_interest_balance_volume": 50},
        {"date": "2026-06-23", "institutional_investors": "自營商",
         "long_open_interest_balance_volume": 30, "short_open_interest_balance_volume": 20},
    ]
    monkeypatch.setattr(cd, "_finmind", lambda *a, **k: rows)
    out = cd.fetch_futures_oi(20)
    assert len(out) == 1
    r = out[0]
    assert r["foreign"] == -700.0
    assert r["trust"] == 550.0
    assert r["dealer"] == 10.0


def test_market_institutional_yi_conversion_and_dealer_merge(monkeypatch):
    rows = [
        {"date": "2026-06-23", "name": "Foreign_Investor", "buy": 599780997370, "sell": 638373350011},
        {"date": "2026-06-23", "name": "Investment_Trust", "buy": 63398318278, "sell": 56162360476},
        {"date": "2026-06-23", "name": "Dealer_self", "buy": 8793363407, "sell": 11432867252},
        {"date": "2026-06-23", "name": "Dealer_Hedging", "buy": 49542268593, "sell": 63886915972},
        {"date": "2026-06-23", "name": "total", "buy": 1, "sell": 1},   # total 應被忽略
    ]
    monkeypatch.setattr(cd, "_finmind", lambda *a, **k: rows)
    out = cd.fetch_market_institutional(20)
    r = out[0]
    # 外資淨 = (599.78B - 638.37B)/1e8 ≈ -385.92 億
    assert r["foreign"] == pytest.approx(-385.92, abs=0.05)
    # 自營 = (self net) + (hedging net) 合併
    assert r["dealer"] == pytest.approx(((8793363407 - 11432867252) + (49542268593 - 63886915972)) / 1e8, abs=0.05)


def test_pc_ratio_parses_taifex_csv(monkeypatch):
    # 模擬 TAIFEX CSV（Big5 bytes）：表頭 + 兩筆數據；P/C 量比在第 4 欄、未平倉比在第 7 欄
    csv_text = (
        "日期,賣權成交量,買權成交量,買賣權成交量比率,賣權未平倉,買權未平倉,買賣權未平倉比率\n"
        "2026/06/24,394224,460283,85.65,56565,42027,134.59,\n"
        "2026/06/23,171057,208467,82.05,83196,84046,98.99,\n"
    )

    class _Resp:
        content = csv_text.encode("big5")

    monkeypatch.setattr(cd.requests, "get", lambda *a, **k: _Resp())
    out = cd.fetch_pc_ratio(20)
    assert len(out) == 2
    # 依日期遞增排序：06-23 在前
    assert out[0]["date"] == "2026-06-23" and out[0]["pc_oi"] == 98.99 and out[0]["pc_vol"] == 82.05
    assert out[1]["date"] == "2026-06-24" and out[1]["pc_oi"] == 134.59


def test_pc_ratio_skips_header_and_bad_rows(monkeypatch):
    csv_text = "亂碼表頭,a,b,c,d,e,f\n2026/06/24,1,2,3.5,4,5,6.6,\nnot-a-date,x,y,z\n"

    class _Resp:
        content = csv_text.encode("big5")

    monkeypatch.setattr(cd.requests, "get", lambda *a, **k: _Resp())
    out = cd.fetch_pc_ratio(20)
    assert len(out) == 1 and out[0]["date"] == "2026-06-24"


def test_market_overview_shape(monkeypatch):
    monkeypatch.setattr(cd, "fetch_futures_oi", lambda days: [{"date": "d"}])
    monkeypatch.setattr(cd, "fetch_market_institutional", lambda days: [])
    monkeypatch.setattr(cd, "fetch_pc_ratio", lambda days: [])
    o = cd.market_overview(15)
    assert o["days"] == 15
    assert set(o.keys()) >= {"days", "futures_oi", "market_institutional", "pc_ratio", "notes"}


def test_cache_returns_same_object(monkeypatch):
    calls = {"n": 0}

    def _fake(*a, **k):
        calls["n"] += 1
        return [{"date": "2026-06-23", "institutional_investors": "外資",
                 "long_open_interest_balance_volume": 1, "short_open_interest_balance_volume": 0}]

    monkeypatch.setattr(cd, "_finmind", _fake)
    cd.fetch_futures_oi(20)
    cd.fetch_futures_oi(20)   # 第二次應走快取、不再呼叫 _finmind
    assert calls["n"] == 1
