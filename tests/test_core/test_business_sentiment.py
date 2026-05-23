"""Smoke test：business.sentiment (SentimentEngine 事件倒數引擎)。

不發網路請求——把 _fetch_economic_calendar 換成回傳空 list，
只驗 SentimentEngine 介面契約、不 crash。
"""

import pytest

from business import sentiment as biz_sent


def test_sentiment_engine_instantiable():
    eng = biz_sent.SentimentEngine()
    assert eng is not None
    assert hasattr(eng, "event_templates")
    assert hasattr(eng, "current_event")
    assert len(eng.event_templates) > 0


def test_global_sentiment_engine_exists():
    """模組頂層應有 sentiment_engine 全域實例。"""
    assert biz_sent.sentiment_engine is not None
    assert isinstance(biz_sent.sentiment_engine, biz_sent.SentimentEngine)


def test_get_latest_sentiment_handles_empty_calendar(monkeypatch):
    """經濟日曆抓不到時不應 crash，回傳合法 dict。"""
    monkeypatch.setattr(biz_sent, "_fetch_economic_calendar", lambda: [])
    eng = biz_sent.SentimentEngine()
    eng.current_event = None  # 強制無事件
    eng._last_triggered = None

    result = eng.get_latest_sentiment()
    assert isinstance(result, dict)
    assert "current" in result
    assert "scheduled" in result
    assert result["scheduled"] == []


def test_check_event_proximity_returns_none_when_no_events(monkeypatch):
    monkeypatch.setattr(biz_sent, "_fetch_economic_calendar", lambda: [])
    eng = biz_sent.SentimentEngine()
    assert eng.check_event_proximity(minutes=15) is None


def test_apply_sentiment_to_score_no_event_returns_base():
    eng = biz_sent.SentimentEngine()
    eng.current_event = None
    assert eng.apply_sentiment_to_score("BTC/USDT", 50.0) == 50.0


def test_apply_sentiment_to_score_global_event_modifies():
    eng = biz_sent.SentimentEngine()
    eng.current_event = {"impact": "global", "score": 10}
    assert eng.apply_sentiment_to_score("BTC/USDT", 50.0) == 60.0


def test_apply_sentiment_to_score_symbol_match():
    eng = biz_sent.SentimentEngine()
    eng.current_event = {"impact": "BTC/USDT", "score": 20}
    assert eng.apply_sentiment_to_score("BTC/USDT", 50.0) == 70.0


def test_apply_sentiment_to_score_symbol_mismatch_dilutes():
    """非目標 symbol 應該被稀釋成 10% 影響。"""
    eng = biz_sent.SentimentEngine()
    eng.current_event = {"impact": "ETH/USDT", "score": 20}
    # 20 * 0.1 = 2.0
    assert eng.apply_sentiment_to_score("BTC/USDT", 50.0) == pytest.approx(52.0)
