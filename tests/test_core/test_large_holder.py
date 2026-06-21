"""LargeHolderCache (TDCC 大戶持股) 單元測試。

對應 backend/layers/large_holder.py。

設計重點驗證：
- CSV 解析正確（17 級距、大戶=級 15、中大戶=級 12-14）
- ID 歸戶後的 % 直接呈現
- symbol 正規化（.TW 後綴去除）
- 不進評分（純揭露）
- weekly dedupe 邏輯
"""

import datetime
import tempfile
from pathlib import Path

import pytest

from layers.large_holder import (
    LARGE_HOLDER_LEVEL,
    LargeHolderCache,
    MEDIUM_LARGE_LEVELS,
    interpret_concentration,
)


# ── 測試用 CSV fixture ─────────────────────────────────────────

SAMPLE_CSV = """﻿資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%
20260618,2330  ,1,2306149,267183521,1.03
20260618,2330  ,2,431365,823536228,3.17
20260618,2330  ,3,50376,361320665,1.39
20260618,2330  ,4,16627,204063151,0.78
20260618,2330  ,5,7823,137653077,0.53
20260618,2330  ,6,7523,184336978,0.71
20260618,2330  ,7,3547,122993695,0.47
20260618,2330  ,8,2035,91839820,0.35
20260618,2330  ,9,4018,281135311,1.08
20260618,2330  ,10,1998,279647764,1.07
20260618,2330  ,11,1334,374361057,1.44
20260618,2330  ,12,548,268864579,1.03
20260618,2330  ,13,342,236381592,0.91
20260618,2330  ,14,223,198941324,0.76
20260618,2330  ,15,1484,22100113305,85.22
20260618,2330  ,16,2,2000,0.00
20260618,2330  ,17,2835392,25932370067,100.00
20260618,0050  ,1,2999000,40000000,1.79
20260618,0050  ,15,150,606000000,27.16
20260618,0050  ,17,3179622,2236000000,100.00
"""


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """LargeHolderCache 寫入 / 讀取都用 tmp_path、不污染專案。"""
    import layers.large_holder as mod
    monkeypatch.setattr(mod, "CACHE_DIR", tmp_path)
    # 重置 singleton
    monkeypatch.setattr(mod, "_cache", None)
    return tmp_path


def _write_sample_csv(tmp_path: Path, date: datetime.date = None) -> Path:
    d = date or datetime.date(2026, 6, 18)
    p = tmp_path / f"tdcc_{d.isoformat()}.csv"
    p.write_text(SAMPLE_CSV, encoding="utf-8")
    return p


# ── CSV 解析正確 ─────────────────────────────────────────

def test_csv_parse_extracts_2330_correctly(tmp_cache):
    _write_sample_csv(tmp_cache)
    cache = LargeHolderCache()
    info = cache.get("2330")
    assert info is not None
    assert info["large_pct"] == 85.22         # 級 15
    assert info["large_holders"] == 1484
    assert info["total_holders"] == 2835392
    # 中大戶 = 級 12+13+14 = 1.03 + 0.91 + 0.76 = 2.70
    assert info["medium_large_pct"] == pytest.approx(2.70, abs=0.01)
    # 合計 >400 張 = 85.22 + 2.70
    assert info["large_plus_medium_pct"] == pytest.approx(87.92, abs=0.01)


def test_csv_parse_extracts_etf_correctly(tmp_cache):
    """0050 ETF — 散戶最愛、大戶 % 應該偏低。"""
    _write_sample_csv(tmp_cache)
    cache = LargeHolderCache()
    info = cache.get("0050")
    assert info is not None
    assert info["large_pct"] == 27.16


def test_unknown_symbol_returns_none(tmp_cache):
    _write_sample_csv(tmp_cache)
    cache = LargeHolderCache()
    assert cache.get("9999") is None


# ── symbol 正規化 ─────────────────────────────────────────

def test_normalize_strips_tw_suffix(tmp_cache):
    _write_sample_csv(tmp_cache)
    cache = LargeHolderCache()
    assert cache.get("2330.TW") == cache.get("2330")
    assert cache.get("2330.TWO") == cache.get("2330")


# ── meta ─────────────────────────────────────────

def test_snapshot_meta_reports_fetch_date(tmp_cache):
    _write_sample_csv(tmp_cache)
    cache = LargeHolderCache()
    meta = cache.snapshot_meta()
    assert meta["fetch_date"] == "2026-06-18"
    assert meta["symbol_count"] == 2  # 2330 + 0050


def test_empty_cache_dir_returns_empty_meta(tmp_cache):
    cache = LargeHolderCache()
    meta = cache.snapshot_meta()
    assert meta == {"fetch_date": None, "symbol_count": 0}


# ── weekly dedupe ─────────────────────────────────────────

def test_fetch_dedupes_within_same_week(tmp_cache, monkeypatch):
    """同週內呼叫 fetch() 第二次應該回 False（不重抓）。"""
    # 先 inject 一個本週已抓的 cache state
    today = datetime.date.today()
    _write_sample_csv(tmp_cache, today)
    cache = LargeHolderCache()
    assert cache._fetch_date == today

    # mock requests.get 確保沒被呼叫
    call_count = [0]
    def fake_get(*args, **kwargs):
        call_count[0] += 1
        raise RuntimeError("不該被 call")
    import layers.large_holder as mod
    monkeypatch.setattr(mod.requests, "get", fake_get)

    # 本週已有 → 應該 skip
    assert cache.fetch() is False
    assert call_count[0] == 0


# ── concentration 文字描述 ─────────────────────────────────────────

@pytest.mark.parametrize("pct,exp", [
    (95.0, "極度集中"),
    (85.0, "極度集中"),
    (84.9, "集中"),
    (70.0, "集中"),
    (69.9, "中等"),
    (50.0, "中等"),
    (49.9, "散戶為主"),
    (10.0, "散戶為主"),
])
def test_interpret_concentration_thresholds(pct, exp):
    assert interpret_concentration(pct) == exp


# ── 級距常量 sanity ─────────────────────────────────────────

def test_constants_are_correct():
    """確認大戶級距 = 15、中大戶 = 12-14。"""
    assert LARGE_HOLDER_LEVEL == "15"
    assert MEDIUM_LARGE_LEVELS == ("12", "13", "14")
