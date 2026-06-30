"""Smoke test：notifier (Telegram) 契約。

不發真的訊息——用 monkeypatch 攔 requests.post，驗證：
1. payload 結構正確（含 chat_id / text / parse_mode）
2. 多 chat_id（逗號分隔）會逐一送出
3. 環境變數沒設時安靜跳過、回傳 False
4. notify_trade 的訊息含關鍵欄位
"""

import logging
import os
import pytest

import notifier


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """重試會 time.sleep，測試中設為 no-op，避免真的等待拖慢測試。"""
    monkeypatch.setattr(notifier.time, "sleep", lambda *_a, **_k: None)


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


def test_send_telegram_retries_until_success(monkeypatch):
    """前 2 次失敗、第 3 次成功 → 回 True，共試 3 次。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _patch_settings_no_override(monkeypatch)

    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        return _FakeResp(200) if calls["n"] >= 3 else _FakeResp(500, "err")

    monkeypatch.setattr(notifier.requests, "post", flaky)
    assert notifier.send_telegram("x") is True
    assert calls["n"] == 3


def test_send_telegram_retries_then_gives_up(monkeypatch):
    """全失敗 → 試 attempts 次（len(_SEND_BACKOFF)+1）後放棄、回 False。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _patch_settings_no_override(monkeypatch)

    calls = {"n": 0}

    def always_fail(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("net down")

    monkeypatch.setattr(notifier.requests, "post", always_fail)
    assert notifier.send_telegram("x") is False
    assert calls["n"] == len(notifier._SEND_BACKOFF) + 1


def test_send_telegram_first_try_no_retry(monkeypatch):
    """首次成功 → 只 post 一次（不重試、不 sleep）。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _patch_settings_no_override(monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(notifier.requests, "post",
                        lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1) or _FakeResp(200))
    assert notifier.send_telegram("x") is True
    assert calls["n"] == 1


def test_failure_log_redacts_token(monkeypatch, caplog):
    """失敗 log 不可含 bot token（含 token 的例外 URL 要被遮蔽）。"""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRETTOKEN123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    _patch_settings_no_override(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("HTTPSConnectionPool url /botSECRETTOKEN123/sendMessage failed")

    monkeypatch.setattr(notifier.requests, "post", boom)
    with caplog.at_level(logging.WARNING):
        notifier.send_telegram("x")
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "SECRETTOKEN123" not in blob       # token 不可外漏到 log
    assert "<bot-token>" in blob               # 已遮蔽


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
