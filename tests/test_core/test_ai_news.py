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


def test_summarize_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert an.summarize_with_gemini([{"title": "t", "link": "l", "source": "s"}]) is None


def test_summarize_parses_gemini_json(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "FAKEKEY")

    class _Resp:
        status_code = 200
        def json(self):
            return {"candidates": [{"content": {"parts": [{
                "text": "```json\n[{\"idx\": 1, \"zh\": \"重點一\"}, {\"idx\": 99, \"zh\": \"越界丟棄\"}]\n```"
            }]}}]}

    captured = {}
    def fake_post(url, headers=None, json=None, timeout=None):
        captured["headers"] = headers
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(an.requests, "post", fake_post)
    items = [{"title": "t0", "link": "l0", "source": "s"},
             {"title": "t1", "link": "l1", "source": "s"}]
    out = an.summarize_with_gemini(items)
    assert out == [{"title": "t1", "link": "l1", "source": "s", "zh": "重點一"}]
    # key 走 header、不在 URL（避免例外訊息帶 key）
    assert captured["headers"]["x-goog-api-key"] == "FAKEKEY"
    assert "FAKEKEY" not in captured["url"]


def test_summarize_http_error_returns_none(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "FAKEKEY")

    class _Resp:
        status_code = 429
        text = "quota"
    monkeypatch.setattr(an.requests, "post", lambda *a, **k: _Resp())
    assert an.summarize_with_gemini([{"title": "t", "link": "l", "source": "s"}]) is None


def test_build_digest_escapes_html():
    items = [{"title": "A <b>bold</b> & risky", "link": "https://x/1?a=1&b=2", "source": "s"}]
    msg = an.build_digest(items, None)
    assert "<b>bold</b>" not in msg          # 標題內 HTML 被 escape
    assert "&lt;b&gt;" in msg
    assert "純標題版" in msg


def test_build_digest_escapes_double_quote_in_href():
    """URL 含雙引號必須被 escape，否則 href 屬性被截斷、Telegram 拒收整則（review 回歸）。"""
    items = [{"title": "T", "link": 'https://x/1?q="broken"', "source": "s"}]
    msg = an.build_digest(items, None)
    assert '"broken"' not in msg
    assert "&quot;broken&quot;" in msg


def test_build_digest_respects_length_limit():
    """超長內容要截斷在 TG_MSG_LIMIT 內（少放幾則），避免 Telegram 4096 拒收。"""
    items = [{"title": "T" * 300, "link": "https://x/" + "a" * 300, "source": "s"}
             for _ in range(10)]
    msg = an.build_digest(items, None)
    assert len(msg) <= an.TG_MSG_LIMIT
    assert "1." in msg          # 至少放得下第一則


def test_build_digest_with_summary():
    s = [{"title": "T", "link": "https://x/1", "source": "OpenAI", "zh": "一行重點"}]
    msg = an.build_digest([], s)
    assert "一行重點" in msg and "OpenAI" in msg and "純標題版" not in msg
