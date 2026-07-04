"""etf_universe（ETF 兩榜 + 標籤）單元測試——不打網路。"""

import json
import os
import sys

import pytest

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from layers import etf_universe as eu


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(eu, "BEAT_CACHE_PATH", tmp_path / "beat.json")


# ── 台股型過濾 ──

def test_filter_excludes_bond_leveraged_inverse_foreign():
    assert eu._is_tw_equity_etf("0050", "元大台灣50") is True
    assert eu._is_tw_equity_etf("00981A", "主動統一台股增長") is True
    assert eu._is_tw_equity_etf("009816", "凱基台灣TOP50") is True
    # 債券（B 尾）/ 槓桿（L）/ 反向（R）
    assert eu._is_tw_equity_etf("00679B", "元大美債20年") is False
    assert eu._is_tw_equity_etf("00631L", "元大台灣50正2") is False
    assert eu._is_tw_equity_etf("00632R", "元大台灣50反1") is False
    # 海外 / 商品（名稱）
    assert eu._is_tw_equity_etf("00646", "元大S&P500") is False
    assert eu._is_tw_equity_etf("00645", "富邦日本") is False
    assert eu._is_tw_equity_etf("00635U", "期元大S&P黃金") is False
    assert eu._is_tw_equity_etf("0061", "元大寶滬深") is False


# ── AUM 榜完整性 ──

def test_top_aum_list_integrity():
    assert len(eu.TOP_AUM_ETFS) == 10
    codes = [e["code"] for e in eu.TOP_AUM_ETFS]
    assert len(set(codes)) == 10                       # 無重複
    assert all(e["aum_billion"] > 0 for e in eu.TOP_AUM_ETFS)
    # 依規模遞減（人工建檔時的排序檢查）
    aums = [e["aum_billion"] for e in eu.TOP_AUM_ETFS]
    assert aums == sorted(aums, reverse=True)


# ── 半年報酬 ──

def test_six_month_return_computation(monkeypatch):
    rows = [{"date": "2026-01-05", "close": 100.0},
            {"date": "2026-07-03", "close": 118.0}]
    monkeypatch.setattr(eu, "_fm_get", lambda *a, **k: rows)
    assert eu._six_month_return("0050", "2026-01-03") == pytest.approx(18.0)


def test_six_month_return_skips_newly_listed(monkeypatch):
    # 首筆在 5.5 個月門檻之後（上市不足半年）→ None
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    rows = [{"date": recent, "close": 20.0},
            {"date": datetime.now().strftime("%Y-%m-%d"), "close": 30.0}]
    monkeypatch.setattr(eu, "_fm_get", lambda *a, **k: rows)
    assert eu._six_month_return("00999", "2026-01-03") is None


# ── 贏大盤榜 + 快取 ──

def test_beat_taiex_top10_computes_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_compute():
        calls["n"] += 1
        return {"date": eu.datetime.now().strftime("%Y-%m-%d"),
                "taiex_ret": 18.0,
                "top": [{"code": "0052", "name": "富邦科技", "ret6m_pct": 25.0}]}

    monkeypatch.setattr(eu, "_compute_beat_taiex_top10", fake_compute)
    r1 = eu.get_beat_taiex_top10()
    r2 = eu.get_beat_taiex_top10()          # 第二次走快取
    assert calls["n"] == 1
    assert r1["top"][0]["code"] == "0052" and r2 == r1


def test_beat_failure_keeps_old_cache(monkeypatch):
    # 先寫好快取（昨天的）
    old = {"date": "2000-01-01", "taiex_ret": 10.0,
           "top": [{"code": "0050", "name": "元大台灣50", "ret6m_pct": 12.0}]}
    eu.BEAT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    eu.BEAT_CACHE_PATH.write_text(json.dumps(old, ensure_ascii=False))
    # 今日計算失敗（空榜）→ 應回舊快取、且不覆蓋
    monkeypatch.setattr(eu, "_compute_beat_taiex_top10",
                        lambda: {"date": "x", "taiex_ret": None, "top": []})
    out = eu.get_beat_taiex_top10()
    assert out["top"][0]["code"] == "0050"
    assert json.loads(eu.BEAT_CACHE_PATH.read_text())["date"] == "2000-01-01"


# ── 標籤 ──

def test_get_etf_tags_both_lists(monkeypatch):
    monkeypatch.setattr(eu, "get_beat_taiex_top10", lambda force=False: {
        "date": "d", "taiex_ret": 18.0,
        "top": [{"code": "00981A", "name": "主動統一台股增長", "ret6m_pct": 29.9}]})
    tags = eu.get_etf_tags("00981A.TW")
    names = [t["tag"] for t in tags]
    assert "規模Top10" in names          # AUM 榜第 6
    assert "贏大盤Top10" in names
    assert "主動嚴選" in names            # BEAT_ETFS 成員
    # detail 帶數值
    aum = next(t for t in tags if t["tag"] == "規模Top10")
    assert "2,975億" in aum["detail"]


def test_get_etf_tags_non_member(monkeypatch):
    monkeypatch.setattr(eu, "get_beat_taiex_top10",
                        lambda force=False: {"date": "d", "taiex_ret": 18.0, "top": []})
    assert eu.get_etf_tags("2330.TW") == []


def test_universe_extension_union(monkeypatch):
    monkeypatch.setattr(eu, "get_beat_taiex_top10", lambda force=False: {
        "date": "d", "taiex_ret": 18.0,
        "top": [{"code": "0052", "name": "富邦科技", "ret6m_pct": 25.0},   # AUM 榜也有 → 去重
                {"code": "00905", "name": "FT臺灣Smart", "ret6m_pct": 20.0}]})
    ext = eu.get_universe_extension()
    assert ext["0050.TW"] == "元大台灣50"
    assert ext["00905.TW"] == "FT臺灣Smart"
    assert len([k for k in ext if k == "0052.TW"]) == 1
    assert len(ext) == 11                 # 10 AUM + 1 新贏大盤（0052 重複去重）


def test_filter_excludes_taiwan_korea_and_us_heavy_actives():
    """實跑抓到的漏洞回歸：臺韓混合型 + 名稱看不出的美股型主動 ETF。"""
    assert eu._is_tw_equity_etf("00735", "國泰臺韓科技") is False
    assert eu._is_tw_equity_etf("00990A", "主動元大AI新經濟") is False   # 美股為主(memory)
    assert eu._is_tw_equity_etf("00988A", "主動統一全球創新") is False
    assert eu._is_tw_equity_etf("00991A", "主動復華未來50") is True      # 台股主動、應保留


def test_compute_aborts_on_low_fetch_success(monkeypatch):
    """限流防護回歸：抓取成功率 <50%（配額被吃掉）→ 回空榜、不產出殘缺結果。"""
    def fake_fm(dataset, **kw):
        if dataset == "TaiwanStockInfo":
            return [{"stock_id": f"009{i:02d}", "stock_name": f"台灣ETF{i}",
                     "type": "twse", "industry_category": "ETF"} for i in range(10)]
        if kw.get("data_id") == "TAIEX":
            return [{"date": "2026-01-05", "close": 100.0},
                    {"date": "2026-07-03", "close": 110.0}]
        return []   # 全部個股抓取失敗（模擬限流）
    monkeypatch.setattr(eu, "_fm_get", fake_fm)
    out = eu._compute_beat_taiex_top10()
    assert out["top"] == [] and out["taiex_ret"] is None


def test_categorize_picks_includes_etf_lists_card(monkeypatch):
    """有標籤的 ETF 必須全數出現在「ETF 兩榜精選」分類卡（不必擠進綜合名次）。"""
    import screener as sc
    fake_results = []
    for i, (sym, tags) in enumerate([
        ("0050.TW", [{"tag": "規模Top10", "detail": "22,473億"}]),
        ("2330.TW", []),
        ("00913.TW", [{"tag": "贏大盤Top10", "detail": "+114.5%"}]),
    ]):
        fake_results.append({
            "symbol": sym, "name": f"n{i}", "composite": 90 - i,
            "scores": {"technical": 50}, "raw_scores": {}, "highlights": [],
            "details": {}, "etf_tags": tags,
        })
    monkeypatch.setattr(sc, "_load_rank_history", lambda: {})
    monkeypatch.setattr(sc, "_update_rank_history_for_category",
                        lambda h, c, s, t: {x: 1 for x in s})
    monkeypatch.setattr(sc, "_save_rank_history", lambda h: None)
    cats = sc.categorize_picks(fake_results)
    etf_cat = next((c for c in cats if c["id"] == "etf_lists"), None)
    assert etf_cat is not None
    syms = [s["symbol"] for s in etf_cat["stocks"]]
    assert syms == ["0050.TW", "00913.TW"]          # 兩檔都在、2330 不在
    assert etf_cat["stocks"][0]["etf_tags"][0]["tag"] == "規模Top10"
