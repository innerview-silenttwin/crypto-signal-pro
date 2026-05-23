"""Smoke test：notifier (Telegram) 契約。

不發真的訊息——用 monkeypatch 攔 requests.post，驗證：
1. payload 結構正確（含 chat_id / text / parse_mode）
2. 多 chat_id（逗號分隔）會逐一送出
3. 環境變數沒設時安靜跳過、回傳 False
4. notify_trade 的訊息含關鍵欄位
"""

import os
import pytest

import notifier


class _FakeResp:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


def _patch_settings_no_override(monkeypatch):
    """避免 settings_manager 把 chat_id 改掉，固定走環境變數那條。"""
    def _raise(*a, **kw):
        raise RuntimeError("disabled in test")
    monkeypatch.setattr(notifier, "_get_config", _get_config_test_factory(monkeypatch), raising=True)


def _get_config_test_factory(monkeypatch):
    def _stub():
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        return token, chat_id
    return _stub


def test_send_telegram_skips_when_unconfigured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    _patch_settings_no_override(monkeypatch)
    sent = []
    monkeypatch.setattr(notifier.requests, "post", lambda *a, **kw: sent.append((a, kw)) or _FakeResp())

    assert notifier.send_telegram("test") is False
    assert sent == [], "未設定時不應該打 API"


def test_send_telegram_posts_with_correct_payload(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _patch_settings_no_override(monkeypatch)

    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp(200)

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    ok = notifier.send_telegram("hello world")

    assert ok is True
    assert "TESTTOKEN" in captured["url"]
    assert captured["json"]["chat_id"] == "12345"
    assert captured["json"]["text"] == "hello world"
    assert captured["json"]["parse_mode"] == "HTML"
    assert captured["timeout"] == 10


def test_send_telegram_handles_multi_chat_ids(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111,222,333")
    _patch_settings_no_override(monkeypatch)

    sent_chats = []

    def fake_post(url, json=None, **kw):
        sent_chats.append(json["chat_id"])
        return _FakeResp(200)

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    ok = notifier.send_telegram("multi")

    assert ok is True
    assert sent_chats == ["111", "222", "333"]


def test_send_telegram_returns_false_on_all_failures(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _patch_settings_no_override(monkeypatch)

    monkeypatch.setattr(
        notifier.requests, "post",
        lambda *a, **kw: _FakeResp(500, "internal error"),
    )
    assert notifier.send_telegram("fail") is False


def test_send_telegram_swallows_network_exception(monkeypatch):
    """網路 raise 時不應該往上拋——只 log，回 False。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _patch_settings_no_override(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(notifier.requests, "post", boom)
    # 不應該 raise
    assert notifier.send_telegram("boom") is False


def test_notify_trade_buy_payload_includes_key_fields(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _patch_settings_no_override(monkeypatch)

    captured = {}

    def fake_post(url, json=None, **kw):
        captured["text"] = json["text"]
        return _FakeResp(200)

    monkeypatch.setattr(notifier.requests, "post", fake_post)

    notifier.notify_trade(
        sector_name="半導體", symbol="2330.TW", stock_name="台積電",
        trade_type="BUY", price=1000.0, qty=1000,
        signal_desc="F3 進場", broker="sinopac",
    )
    txt = captured["text"]
    assert "買入通知" in txt
    assert "台積電" in txt
    assert "2330" in txt
    assert "1000.00" in txt
    assert "永豐" in txt  # broker tag


def test_notify_trade_sell_includes_pnl(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _patch_settings_no_override(monkeypatch)

    captured = {}

    def fake_post(url, json=None, **kw):
        captured["text"] = json["text"]
        return _FakeResp(200)

    monkeypatch.setattr(notifier.requests, "post", fake_post)

    notifier.notify_trade(
        sector_name="半導體", symbol="2330.TW", stock_name="台積電",
        trade_type="SELL", price=1100.0, qty=1000,
        signal_desc="S1 停利", broker="virtual",
        profit=100_000, profit_pct=10.0,
    )
    txt = captured["text"]
    assert "賣出通知" in txt
    assert "100,000" in txt or "100000" in txt  # profit amount
    assert "+10.00%" in txt
    assert "虛擬" in txt
