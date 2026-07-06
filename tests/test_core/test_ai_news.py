"""ai_news 單元測試（不打網路、不發訊息）。"""

import os
import sys

import pytest

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import ai_news as an


@pytest.fixture(autouse=True)
def _tmp_seen(tmp_path, monkeypatch):
    """seen store 導到 tmp，不汙染真檔。"""
    monkeypatch.setattr(an, "SEEN_PATH", tmp_path / "seen.json")


def test_parse_rss_item_format():
    xml = """<rss><channel>
        <item><title>GPT-6 released</title><link>https://x/1</link></item>
        <item><title>no link ok</title></item>
    </channel></rss>"""
    out = an._parse_rss(xml, "SRC")
    assert out[0] == {"title": "GPT-6 released", "link": "https://x/1", "source": "SRC"}
    assert len(out) == 2  # 無 link 仍保留（用標題去重）


def test_parse_rss_atom_format():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>Atom entry</title><link href="https://x/a"/></entry>
    </feed>"""
    out = an._parse_rss(xml, "SRC")
    assert out == [{"title": "Atom entry", "link": "https://x/a", "source": "SRC"}]


def test_parse_rss_bad_xml_returns_empty():
    assert an._parse_rss("not xml <<", "SRC") == []


def test_keyword_filter_matches():
    assert an._KW_RE.search("New LLM benchmark")
    assert an._KW_RE.search("Anthropic ships something")
    assert an._KW_RE.search("Why AI agents fail")
    assert not an._KW_RE.search("Rust borrow checker deep dive")


def test_keyword_filter_word_boundary_no_false_positive():
    """'ai' 需字界——不可誤匹配 chair/email/Airbnb（review 抓到的 bug 回歸）。"""
    for t in ["Repair your email workflow", "The chair design of 2026",
              "Maintaining legacy code", "Show HN: Airbnb clone"]:
        assert not an._KW_RE.search(t), t
    assert an._KW_RE.search("Fine-tuning at scale")  # 變體仍要中


def test_filter_fresh_dedups_and_persists():
    items = [
        {"title": "A", "link": "https://x/1", "source": "s"},
        {"title": "A dup", "link": "https://x/1", "source": "s"},   # 同 link 批內去重
        {"title": "B", "link": "https://x/2", "source": "s"},
    ]
    out1 = an.filter_fresh(items, now=1000.0)
    assert [i["link"] for i in out1] == ["https://x/1", "https://x/2"]
    # 第二輪：全部已看過 → 空
    out2 = an.filter_fresh(items, now=2000.0)
    assert out2 == []


def test_filter_fresh_ttl_expiry():
    an.filter_fresh([{"title": "old", "link": "https://x/old", "source": "s"}], now=1000.0)
    # TTL(14天) 過後同 link 視為新
    later = 1000.0 + (an.SEEN_TTL_DAYS + 1) * 86400
    out = an.filter_fresh([{"title": "old", "link": "https://x/old", "source": "s"}], now=later)
    assert len(out) == 1


def test_briefing_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert an.briefing_with_gemini([{"title": "t", "link": "l", "source": "s"}]) is None


def test_briefing_parses_and_clamps(monkeypatch):
    """解析結構 + label 白名單（怪 label 降為臆測）+ grounding 標記。"""
    payload = {
        "sections": [{"sector": "AI 模型與應用",
                      "items": [{"label": "已證實", "text": "OpenAI 發布 X"},
                                {"label": "小道消息", "text": "怪標籤要降級"}]}],
        "social": [{"who": "美國總統", "label": "報導", "text": "發言 Y"}],
        "fed": [{"label": "臆測", "text": "市場解讀 Z"}],
        "outlook": {"horizon": "1-3個月",
                    "scenarios": [{"name": "續漲", "probability_pct": 55, "rationale": "動能"},
                                  {"name": "回檔", "probability_pct": 45, "rationale": "估值"}]},
    }
    import json as _json
    monkeypatch.setattr(an, "_gemini_generate",
                        lambda p, use_search, timeout=90: "```json\n" + _json.dumps(payload, ensure_ascii=False) + "\n```" if use_search else None)
    b = an.briefing_with_gemini([{"title": "t", "link": "l", "source": "s"}])
    assert b["grounded"] is True
    assert b["sections"][0]["items"][0]["label"] == "已證實"
    assert b["sections"][0]["items"][1]["label"] == "臆測"     # 白名單降級
    assert b["social"][0]["who"] == "美國總統"
    assert b["outlook"]["scenarios"][0]["probability_pct"] == 55


def test_briefing_falls_back_to_no_search(monkeypatch):
    calls = []
    def fake(p, use_search, timeout=90):
        calls.append(use_search)
        if use_search:
            return None
        return '{"sections": [{"sector": "其他", "items": [{"label": "報導", "text": "純資料版"}]}], "social": [], "fed": [], "outlook": null}'
    monkeypatch.setattr(an, "_gemini_generate", fake)
    b = an.briefing_with_gemini([{"title": "t", "link": "l", "source": "s"}])
    assert calls == [True, False]
    assert b["grounded"] is False and b["sections"]


def test_briefing_bad_json_returns_none(monkeypatch):
    monkeypatch.setattr(an, "_gemini_generate", lambda *a, **k: "抱歉無法完成")
    assert an.briefing_with_gemini([{"title": "t", "link": "l", "source": "s"}]) is None


def test_briefing_unparseable_grounded_retries_without_search(monkeypatch):
    """搜尋版回非 JSON（refusal）也要退純文字版重試，不是只有連線失敗才退。"""
    calls = []
    def fake(p, use_search, timeout=90):
        calls.append(use_search)
        if use_search:
            return "我不能協助這個請求"   # 200 但不可解析
        return '{"sections": [{"sector": "其他", "items": [{"label": "報導", "text": "ok"}]}]}'
    monkeypatch.setattr(an, "_gemini_generate", fake)
    b = an.briefing_with_gemini([{"title": "t", "link": "l", "source": "s"}])
    assert calls == [True, False]
    assert b and b["grounded"] is False


def test_briefing_tolerates_bad_probability_pct(monkeypatch):
    """probability_pct 是 "40%"/null 不可炸掉整份簡報（review 抓到的 bug 回歸）。"""
    payload = ('{"sections": [{"sector": "其他", "items": [{"label": "報導", "text": "X"}]}], '
               '"outlook": {"horizon": "1-3個月", "scenarios": ['
               '{"name": "A", "probability_pct": "40%", "rationale": "r"}, '
               '{"name": "B", "probability_pct": null, "rationale": "r"}]}}')
    monkeypatch.setattr(an, "_gemini_generate", lambda p, use_search, timeout=90: payload)
    b = an.briefing_with_gemini([{"title": "t", "link": "l", "source": "s"}])
    assert b is not None and b["sections"]                      # 簡報保住
    assert b["outlook"]["scenarios"][0]["probability_pct"] == 40  # "40%" 容錯
    assert b["outlook"]["scenarios"][1]["probability_pct"] == 0   # null 容錯


def test_briefing_all_empty_after_clean_returns_none(monkeypatch):
    """sections 存在但 items 全缺 text → 清洗後全空要回 None，別發空殼訊息。"""
    payload = '{"sections": [{"sector": "其他", "items": [{"label": "報導"}]}], "social": [], "fed": []}'
    monkeypatch.setattr(an, "_gemini_generate", lambda *a, **k: payload)
    assert an.briefing_with_gemini([{"title": "t", "link": "l", "source": "s"}]) is None


def test_briefing_prompt_reserves_fed_slots(monkeypatch):
    """80 則截斷不可把排在後面的 Fed 源切掉（已標 seen 會永久遺失）。"""
    captured = {}
    def fake(p, use_search, timeout=90):
        captured["prompt"] = p
        return None
    monkeypatch.setattr(an, "_gemini_generate", fake)
    items = [{"title": f"ai {i}", "link": f"https://x/{i}", "source": "TechCrunch"}
             for i in range(90)]
    items += [{"title": "FOMC statement", "link": "https://fed/1", "source": "Fed 新聞稿"},
              {"title": "Powell speech", "link": "https://fed/2", "source": "Fed 演說"}]
    an.briefing_with_gemini(items)
    assert "FOMC statement" in captured["prompt"]
    assert "Powell speech" in captured["prompt"]


def test_gemini_generate_key_in_header_not_url(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "FAKEKEY")
    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["headers"] = headers
        captured["url"] = url
        captured["body"] = json
        return _Resp()

    monkeypatch.setattr(an.requests, "post", fake_post)
    assert an._gemini_generate("hi", use_search=True) == "ok"
    assert captured["headers"]["x-goog-api-key"] == "FAKEKEY"
    assert "FAKEKEY" not in captured["url"]
    assert captured["body"].get("tools") == [{"google_search": {}}]


def test_build_digest_escapes_html():
    items = [{"title": "A <b>bold</b> & risky", "link": "https://x/1?a=1&b=2", "source": "s"}]
    msg = an.build_digest(items)
    assert "<b>bold</b>" not in msg
    assert "&lt;b&gt;" in msg
    assert "純標題版" in msg


def test_build_digest_escapes_double_quote_in_href():
    items = [{"title": "T", "link": 'https://x/1?q="broken"', "source": "s"}]
    msg = an.build_digest(items)
    assert '"broken"' not in msg and "&quot;broken&quot;" in msg


def test_build_digest_respects_length_limit():
    items = [{"title": "T" * 300, "link": "https://x/" + "a" * 300, "source": "s"}
             for _ in range(10)]
    msg = an.build_digest(items)
    assert len(msg) <= an.TG_MSG_LIMIT and "1." in msg


def test_format_briefing_chunks_and_escapes():
    """段落裝箱：超長切多則 + 頁碼；內容 escape；免責聲明在趨勢段。"""
    b = {
        "grounded": True,
        "sections": [{"sector": "AI <模型>", "items": [{"label": "已證實", "text": "T" * 1500}]}
                     for _ in range(4)],
        "social": [{"who": "某CEO", "label": "報導", "text": "說了 <b>大話</b>"}],
        "fed": [{"label": "已證實", "text": "維持利率"}],
        "outlook": {"horizon": "1-3個月",
                    "scenarios": [{"name": "續漲", "probability_pct": 60, "rationale": "R"}]},
    }
    msgs = an.format_briefing(b)
    assert len(msgs) >= 2                                  # 超長 → 多則
    assert all(len(m) <= an.TG_MSG_LIMIT + 30 for m in msgs)  # 頁碼小加量
    joined = "\n".join(msgs)
    assert "(1/" in joined                                 # 有頁碼
    assert "&lt;模型&gt;" in joined and "<b>大話</b>" not in joined   # escape
    assert "非投資建議" in joined
    assert "60%" in joined


def test_format_briefing_single_message_no_pagination():
    b = {"grounded": True,
         "sections": [{"sector": "其他", "items": [{"label": "報導", "text": "短"}]}],
         "social": [], "fed": [], "outlook": None}
    msgs = an.format_briefing(b)
    assert len(msgs) == 1 and "(1/" not in msgs[0]


def test_format_briefing_splits_single_oversized_section():
    """單一段落 escape 後超過上限也要切開，不可產出 >4096 被 Telegram 拒收的訊息。"""
    b = {"grounded": True,
         "sections": [{"sector": "AI",
                       "items": [{"label": "報導", "text": '"&<' * 73}   # escape 後 ~1300 字/則
                                 for _ in range(8)]}],
         "social": [], "fed": [], "outlook": None}
    msgs = an.format_briefing(b)
    assert len(msgs) >= 2
    assert all(len(m) <= an.TG_MSG_LIMIT + 30 for m in msgs)  # 頁碼小加量、仍 < 4096


def test_run_digest_no_fresh_returns_full_shape(monkeypatch):
    """無新項目的 early return 也要含完整鍵（endpoint 回應 shape 一致）。"""
    monkeypatch.setattr(an, "fetch_all_items", lambda: [])
    out = an.run_digest()
    assert out == {"fetched": 0, "fresh": 0, "briefing": False,
                   "grounded": False, "messages": 0, "sent": False}


def test_run_digest_stops_after_first_send_failure(monkeypatch):
    """多則發送第一則失敗即停，別每頁重燒 notifier 整組重試。"""
    import notifier
    items = [{"title": "t", "link": "https://x/1", "source": "s"}]
    monkeypatch.setattr(an, "fetch_all_items", lambda: items)
    monkeypatch.setattr(an, "filter_fresh", lambda x: x)
    # briefing 要是 truthy 才會走 format_briefing 多則路徑（否則 fallback 單則、測不到 break）
    monkeypatch.setattr(an, "briefing_with_gemini", lambda x: {"grounded": True})
    monkeypatch.setattr(an, "format_briefing", lambda b: ["m1", "m2", "m3"])
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda m: (sent.append(m), False)[1])
    out = an.run_digest()
    assert out["messages"] == 3          # 確實走多則路徑（防測試退化成單則假通過）
    assert len(sent) == 1 and out["sent"] is False


def test_safe_pct_variants():
    assert an._safe_pct(55) == 55
    assert an._safe_pct("40%") == 40
    assert an._safe_pct("40-60%") == 40      # 取第一個數字、不可黏成 4060→100
    assert an._safe_pct(None) == 0
    assert an._safe_pct("約七成") == 0        # 無數字 → 0
    assert an._safe_pct(150) == 100          # 夾 0-100
