"""consultation 修正的單元測試（不打網路、不呼叫真 Gemini）。

覆蓋：
1. _gemini_call：key 走 header 不進 URL；無 key 回 None
2. _ai_position_analysis：grounding 成功解析 / 降級鏈 / 解析失敗回 None
3. _compute_live_conditions：正常回 dict、例外回 None
4. consult_position 快取 miss → 走即時計分（分數不再是 50）
"""

import os
import sys

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import consultation as ct


# ── _gemini_call ─────────────────────────────────────────

def test_gemini_call_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert ct._gemini_call("hi", use_search=False) is None


def test_gemini_call_key_in_header_not_url(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRETK")
    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    import requests as _rq

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json
        return _Resp()

    monkeypatch.setattr(_rq, "post", fake_post)
    out = ct._gemini_call("hi", use_search=True)
    assert out == {"text": "ok", "sources": []}
    assert captured["headers"]["x-goog-api-key"] == "SECRETK"
    assert "SECRETK" not in captured["url"]
    assert captured["body"].get("tools") == [{"google_search": {}}]


def test_gemini_call_filters_non_http_sources(monkeypatch):
    """grounding 來源 scheme 白名單——javascript: 等不可進前端 <a href>（review 回歸）。"""
    monkeypatch.setenv("GEMINI_API_KEY", "K")

    class _Resp:
        status_code = 200
        def json(self):
            return {"candidates": [{
                "content": {"parts": [{"text": "ok"}]},
                "groundingMetadata": {"groundingChunks": [
                    {"web": {"title": "good", "uri": "https://ok.example"}},
                    {"web": {"title": "bad", "uri": "javascript:alert(1)"}},
                    {"web": {"title": "empty", "uri": ""}},
                ]}}]}

    import requests as _rq
    monkeypatch.setattr(_rq, "post", lambda *a, **k: _Resp())
    out = ct._gemini_call("hi", use_search=True)
    assert out["sources"] == [{"title": "good", "uri": "https://ok.example"}]


# ── _ai_position_analysis ────────────────────────────────

_ARGS = dict(symbol="2330.TW", name="台積電", current_price=1000.0,
             buy_price=900.0, quantity=2, unrealized_pnl=200000.0,
             unrealized_pnl_pct=11.1,
             conditions={"tech_score": 60, "chip_score": 55, "regime_state": "多頭",
                         "pe": 25, "peg": 1.2, "yoy": 30,
                         "foreign_consec_buy": 3, "trust_consec_buy": 1},
             rec={"recommendation": "持有", "n_matches": 10},
             horizon_stats={"mid": {"avg_return": 2.0, "win_rate": 60}})


def test_ai_analysis_grounded_success(monkeypatch):
    resp = {"text": '前言 {"situation": "局勢OK", "news": ["6/30 法說"], '
                    '"action": "持有", "action_plan": "續抱2張", "risks": ["風險A"]} 後綴',
            "sources": [{"title": "src", "uri": "https://x"}]}
    monkeypatch.setattr(ct, "_gemini_call", lambda p, use_search: resp if use_search else None)
    out = ct._ai_position_analysis(**_ARGS)
    assert out["situation"] == "局勢OK"
    assert out["action"] == "持有"
    assert out["grounded"] is True
    assert out["sources"] == [{"title": "src", "uri": "https://x"}]


def test_ai_analysis_fallback_to_no_search(monkeypatch):
    calls = []

    def fake(prompt, use_search):
        calls.append(use_search)
        if use_search:
            return None  # grounding 失敗
        return {"text": '{"situation": "純資料版", "news": [], "action": "減碼", '
                        '"action_plan": "p", "risks": []}', "sources": []}

    monkeypatch.setattr(ct, "_gemini_call", fake)
    out = ct._ai_position_analysis(**_ARGS)
    assert calls == [True, False]          # 先 grounding、失敗才降級
    assert out["grounded"] is False
    assert out["action"] == "減碼"


def test_ai_analysis_all_fail_returns_none(monkeypatch):
    monkeypatch.setattr(ct, "_gemini_call", lambda *a, **k: None)
    assert ct._ai_position_analysis(**_ARGS) is None


def test_ai_analysis_bad_json_returns_none(monkeypatch):
    monkeypatch.setattr(ct, "_gemini_call",
                        lambda p, use_search: {"text": "抱歉我無法回答", "sources": []})
    assert ct._ai_position_analysis(**_ARGS) is None


def test_ai_analysis_caps_list_lengths(monkeypatch):
    resp = {"text": '{"situation": "s", "news": ["1","2","3","4","5","6"], '
                    '"action": "持有", "action_plan": "p", "risks": ["a","b","c","d"]}',
            "sources": []}
    monkeypatch.setattr(ct, "_gemini_call", lambda p, use_search: resp)
    out = ct._ai_position_analysis(**_ARGS)
    assert len(out["news"]) == 4 and len(out["risks"]) == 3


# ── _compute_live_conditions ─────────────────────────────

def test_compute_live_conditions_wires_scan(monkeypatch):
    import screener
    from layers import fundamental, sentiment
    fake_cond = {"symbol": "9999.TW", "raw_scores": {"technical": 62, "chipflow": 71},
                 "details": {"regime_state": "多頭"}}
    monkeypatch.setattr(screener, "scan_single_stock", lambda s, n, pe, arts: fake_cond)
    monkeypatch.setattr(fundamental, "fetch_twse_pe_all", lambda: {})
    monkeypatch.setattr(sentiment, "fetch_rss_articles", lambda: [])
    out = ct._compute_live_conditions("9999.TW", "測試")
    assert out is fake_cond


def test_compute_live_conditions_exception_returns_none(monkeypatch):
    import screener
    monkeypatch.setattr(screener, "scan_single_stock",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    from layers import fundamental, sentiment
    monkeypatch.setattr(fundamental, "fetch_twse_pe_all", lambda: {})
    monkeypatch.setattr(sentiment, "fetch_rss_articles", lambda: [])
    assert ct._compute_live_conditions("9999.TW", "測試") is None


# ── consult_position 快取 miss → 即時計分（核心回歸：不再全 50）──

def test_consult_position_uses_live_scores_on_cache_miss(monkeypatch):
    live_cond = {
        "symbol": "9999.TW",
        "raw_scores": {"technical": 68.0, "chipflow": 72.0},
        "scores": {"technical": 68.0, "chipflow": 72.0},
        "composite": 70,
        "highlights": [],
        "details": {"regime_state": "多頭", "chipflow": {"foreign_consec_buy": 4,
                                                          "trust_consec_buy": 2}},
    }
    monkeypatch.setattr(ct, "_get_current_conditions", lambda s: None)        # 快取 miss
    monkeypatch.setattr(ct, "_compute_live_conditions", lambda s, n: live_cond)
    monkeypatch.setattr(ct, "_load_perf_cache", lambda: [])
    monkeypatch.setattr(ct, "_get_current_price_from_cache", lambda s: 105.0)
    monkeypatch.setattr(ct, "_ai_position_analysis", lambda **k: None)

    out = ct.consult_position("9999", buy_price=100.0, quantity=1)
    cond = out["current_conditions"]
    assert cond["tech_score"] == 68.0          # 不是 50！
    assert cond["chip_score"] == 72.0
    assert cond["regime_state"] == "多頭"
    assert cond["foreign_consec_buy"] == 4
    assert "五維即時計算" in out["data_source"]
    assert out["ai_analysis"] is None          # 無 key 時欄位為 None、其餘照舊
