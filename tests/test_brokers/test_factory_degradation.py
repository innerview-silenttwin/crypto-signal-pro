"""detect_broker_degradation：偵測「期望永豐、實際降級虛擬」。

背景：2026-06-24 永豐 sim 503 SystemMaintenance，factory silent fallback
VirtualBroker、整天紙上交易，但 heartbeat 仍報健康。此函式讓上層能主動告警。
"""

import os

import pytest

from brokers.factory import detect_broker_degradation


class _FakeSinopac:
    pass


_FakeSinopac.__name__ = "SinopacBroker"


class _FakeVirtual:
    pass


_FakeVirtual.__name__ = "VirtualBroker"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # 預設清掉相關 env，個別 test 自己設
    monkeypatch.delenv("BROKER_MODE", raising=False)
    monkeypatch.delenv("ALLOWED_SECTORS", raising=False)


def test_non_sinopac_mode_never_degraded(monkeypatch):
    """BROKER_MODE != sinopac → 虛擬是預期行為、degraded 永遠空。"""
    monkeypatch.setenv("BROKER_MODE", "virtual")
    r = detect_broker_degradation({"semiconductor": _FakeVirtual()})
    assert r == {"mode": "virtual", "degraded": [], "ok": []}


def test_sinopac_mode_detects_degraded(monkeypatch):
    """白名單內 sector 拿到非 Sinopac broker → 列入 degraded。"""
    monkeypatch.setenv("BROKER_MODE", "sinopac")
    monkeypatch.setenv("ALLOWED_SECTORS", "semiconductor,electronics")
    r = detect_broker_degradation(
        {
            "semiconductor": _FakeVirtual(),  # 降級
            "electronics": _FakeSinopac(),    # 正常
            "finance": _FakeVirtual(),        # 白名單外、不算
        }
    )
    assert r["mode"] == "sinopac"
    assert r["degraded"] == ["semiconductor"]
    assert r["ok"] == ["electronics"]


def test_sinopac_mode_all_ok(monkeypatch):
    monkeypatch.setenv("BROKER_MODE", "sinopac")
    monkeypatch.setenv("ALLOWED_SECTORS", "semiconductor,electronics")
    r = detect_broker_degradation(
        {"semiconductor": _FakeSinopac(), "electronics": _FakeSinopac()}
    )
    assert r["degraded"] == []
    assert set(r["ok"]) == {"semiconductor", "electronics"}


def test_empty_brokers(monkeypatch):
    monkeypatch.setenv("BROKER_MODE", "sinopac")
    monkeypatch.setenv("ALLOWED_SECTORS", "semiconductor")
    r = detect_broker_degradation({})
    assert r["degraded"] == [] and r["ok"] == []


def test_none_brokers_safe(monkeypatch):
    """傳 None 不應炸（broker 還沒 init 的情況）。"""
    monkeypatch.setenv("BROKER_MODE", "sinopac")
    monkeypatch.setenv("ALLOWED_SECTORS", "semiconductor")
    r = detect_broker_degradation(None)
    assert r["degraded"] == [] and r["ok"] == []


def test_mode_case_insensitive(monkeypatch):
    """BROKER_MODE 大小寫 / 空白容錯。"""
    monkeypatch.setenv("BROKER_MODE", "  SINOPAC  ")
    monkeypatch.setenv("ALLOWED_SECTORS", "semiconductor")
    r = detect_broker_degradation({"semiconductor": _FakeVirtual()})
    assert r["mode"] == "sinopac"
    assert r["degraded"] == ["semiconductor"]
