"""/api/internal/* 的 X-Internal-Key auth（item 15）。

驗證重點：
- 未設 CSP_INTERNAL_KEY → fail-open（向後相容，不擋）。
- 設了金鑰 → 缺 header / 錯 header 一律 403，且 403 發生在 handler 之前（不會誤發 Telegram）。
- 設了金鑰 + 正確 header → 放行。

安全：403 的 case handler 不會執行，不觸發真 Telegram；放行的 case 用 monkeypatch
把底層發訊函式換成 no-op。
"""

import pytest
from fastapi.testclient import TestClient

import main


INTERNAL_ENDPOINTS = [
    "/api/internal/trigger-premarket-check",
    "/api/internal/trigger-evening-summary",
    "/api/internal/trigger-daily-inst-refresh",
]


@pytest.fixture
def client(monkeypatch):
    # 把三個底層重活換成 no-op，確保任何「放行」路徑都不會真的發 Telegram / 抓資料。
    monkeypatch.setattr(main, "_compute_data_freshness_report", lambda: {}, raising=False)
    monkeypatch.setattr(main, "_send_premarket_telegram", lambda report: None, raising=False)
    monkeypatch.setattr(main, "_send_evening_summary_telegram", lambda: None, raising=False)
    monkeypatch.setattr(main, "_run_institutional_refresh", lambda: None, raising=False)
    # 不用 with TestClient(...)：避免觸發 startup event（連 broker 等重啟動）。
    return TestClient(main.app)


def test_fail_open_when_key_unset(client, monkeypatch):
    """未設 CSP_INTERNAL_KEY 時不擋（向後相容）。"""
    monkeypatch.delenv("CSP_INTERNAL_KEY", raising=False)
    monkeypatch.setattr(main, "_internal_key_warned", False, raising=False)
    resp = client.post("/api/internal/trigger-evening-summary")
    assert resp.status_code == 200
    assert resp.json().get("status") in ("ok", "error")  # 有進到 handler


@pytest.mark.parametrize("path", INTERNAL_ENDPOINTS)
def test_missing_key_rejected(client, monkeypatch, path):
    """設了金鑰但沒帶 header → 403，三個 endpoint 都要擋。"""
    monkeypatch.setenv("CSP_INTERNAL_KEY", "s3cr3t-key")
    resp = client.post(path)
    assert resp.status_code == 403


@pytest.mark.parametrize("path", INTERNAL_ENDPOINTS)
def test_wrong_key_rejected(client, monkeypatch, path):
    """帶錯 header → 403。"""
    monkeypatch.setenv("CSP_INTERNAL_KEY", "s3cr3t-key")
    resp = client.post(path, headers={"X-Internal-Key": "wrong"})
    assert resp.status_code == 403


def test_non_ascii_header_rejected_not_500(client, monkeypatch):
    """非 ASCII header 應乾淨回 403，而非 hmac.compare_digest 丟 TypeError 變 500。"""
    monkeypatch.setenv("CSP_INTERNAL_KEY", "s3cr3t-key")
    # HTTP header 走 latin-1：用 bytes 送非 ASCII，模擬 server 端會收到的 str（含 é）。
    # 修正前 str 版 compare_digest 對非 ASCII str 會 raise TypeError → 500。
    resp = client.post(
        "/api/internal/trigger-evening-summary",
        headers={"X-Internal-Key": "café".encode("latin-1")},
    )
    assert resp.status_code == 403


def test_correct_key_allowed(client, monkeypatch):
    """帶對 header → 放行（非 403）。"""
    monkeypatch.setenv("CSP_INTERNAL_KEY", "s3cr3t-key")
    resp = client.post(
        "/api/internal/trigger-evening-summary",
        headers={"X-Internal-Key": "s3cr3t-key"},
    )
    assert resp.status_code == 200
    assert resp.json().get("status") in ("ok", "error")
