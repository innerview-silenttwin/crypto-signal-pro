"""TDCC 集保戶大戶持股 — 純揭露用、不進五面評分。

來源：TDCC OpenData https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5
- 每週公布一次（週六/週日更新），CSV 含全市場每股 17 個持股級距
- 級距 15 = 持股 > 1,000 張（即「大戶」）
- 級距以「**ID 歸戶**」計算（同人在多券商開戶會合併）

設計原則（依用戶 2026-06-21 討論）：
- 此模組**只揭露不評分** — 不進 ChipFlowLayer / 五面雷達圖
- 大戶 % 反映「籌碼集中度」，跟既有 chipflow（短期動向）性質不同
- UI 在「籌碼資訊」區塊獨立顯示，避免跟既有法人分數混淆

技術債（待累積歷史後可做）：
- 異常出貨警示（大戶 % 週週掉 > 0.5pp → telegram）
- stable_uptrend audit（連 N 個月減持 → 移名單）
- 同產業 percentile 校正（避免大型股 vs 小型股 raw 數字誤導）

詳見：memory/project_large_holder_disclosure.md（待建）
"""

from __future__ import annotations

import csv
import datetime
import logging
import os
import threading
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# TDCC OpenData CSV：最新一週全市場集保戶股權分散
TDCC_URL = "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"

# 「大戶」級距：>1,000 張
LARGE_HOLDER_LEVEL = "15"

# 「中大戶」級距：>400-1,000 張（持股範圍 400,001 ~ 999,999 股）
# TDCC 級距 14 = 800,001-1,000,000；級 13 = 600,001-800,000；級 12 = 400,001-600,000
MEDIUM_LARGE_LEVELS = ("12", "13", "14")

# Cache file path
_BASE_DIR = Path(__file__).resolve().parents[1]
CACHE_DIR = _BASE_DIR / "data" / "tdcc"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class LargeHolderCache:
    """全市場單期 snapshot in-memory + on-disk cache（per week）。"""

    def __init__(self):
        self._lock = threading.Lock()
        # symbol (4-digit str) → {date, level_pct: {level_str: pct},
        #                          large_pct: float, medium_large_pct: float,
        #                          total_holders: int, large_holders: int}
        self._snapshot: dict[str, dict] = {}
        self._fetch_date: Optional[datetime.date] = None
        self._load_disk_cache()

    @staticmethod
    def _normalize(symbol: str) -> str:
        """去掉 .TW 後綴。"""
        return symbol.split(".")[0].strip()

    def _disk_cache_path(self, fetch_date: datetime.date) -> Path:
        return CACHE_DIR / f"tdcc_{fetch_date.isoformat()}.csv"

    def _load_disk_cache(self):
        """啟動時找最近的 cache csv 載入，避免每次重啟都 re-fetch。"""
        if not CACHE_DIR.exists():
            return
        files = sorted(CACHE_DIR.glob("tdcc_*.csv"), reverse=True)
        if not files:
            return
        latest = files[0]
        try:
            date_str = latest.stem.replace("tdcc_", "")
            fetch_date = datetime.date.fromisoformat(date_str)
            self._parse_csv(latest, fetch_date)
            logger.info("[large_holder] 從 disk 載入 %s (%d symbols)",
                        latest.name, len(self._snapshot))
        except Exception as e:
            logger.warning("[large_holder] disk cache load failed: %r", e)

    def _parse_csv(self, csv_path: Path, fetch_date: datetime.date):
        """解析 TDCC CSV，建立 per-symbol snapshot。

        CSV 格式：資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%
        17 個級距，級 17 = 合計（總人數、總股數）
        """
        # symbol → {level: (people, shares, pct)}
        raw: dict[str, dict] = {}
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader)  # skip header
            for row in reader:
                if len(row) < 6:
                    continue
                date_str, sid_raw, level, people, shares, pct_str = row
                sid = sid_raw.strip()
                try:
                    raw.setdefault(sid, {})[level.strip()] = (
                        int(people or 0), int(shares or 0), float(pct_str or 0.0)
                    )
                except ValueError:
                    continue

        # 整理成 snapshot
        snapshot = {}
        for sid, levels in raw.items():
            # 合計（級 17）
            total = levels.get("17", (0, 0, 0))
            total_people, total_shares, _ = total

            # 大戶（級 15）
            large_people, large_shares, large_pct = levels.get(LARGE_HOLDER_LEVEL, (0, 0, 0.0))

            # 中大戶（級 12-14 合計）
            medium_pct = sum(levels.get(lv, (0, 0, 0.0))[2] for lv in MEDIUM_LARGE_LEVELS)
            medium_people = sum(levels.get(lv, (0, 0, 0.0))[0] for lv in MEDIUM_LARGE_LEVELS)

            snapshot[sid] = {
                "date": fetch_date.isoformat(),
                "total_holders": total_people,
                "total_shares": total_shares,
                "large_pct": round(large_pct, 2),          # >1000 張
                "large_holders": large_people,
                "medium_large_pct": round(medium_pct, 2),  # >400-1000 張
                "medium_large_holders": medium_people,
                "large_plus_medium_pct": round(large_pct + medium_pct, 2),  # >400 張
            }

        with self._lock:
            self._snapshot = snapshot
            self._fetch_date = fetch_date

    def fetch(self, force: bool = False) -> bool:
        """從 TDCC 抓最新 CSV。回 True 表示有更新。

        每週自動 dedupe（若今週已抓過、不重抓）。
        """
        today = datetime.date.today()
        # 本週週一 = today - weekday()
        week_start = today - datetime.timedelta(days=today.weekday())
        if not force and self._fetch_date and self._fetch_date >= week_start:
            return False

        try:
            r = requests.get(TDCC_URL, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                logger.error("[large_holder] TDCC HTTP %d", r.status_code)
                return False
            csv_path = self._disk_cache_path(today)
            csv_path.write_bytes(r.content)
            self._parse_csv(csv_path, today)
            logger.info("[large_holder] 抓 TDCC 成功 %s (%d symbols)",
                        today, len(self._snapshot))
            return True
        except Exception as e:
            logger.error("[large_holder] TDCC fetch failed: %r", e)
            return False

    def get(self, symbol: str) -> Optional[dict]:
        """取得單一 symbol 的大戶資訊；無資料回 None。"""
        sid = self._normalize(symbol)
        with self._lock:
            entry = self._snapshot.get(sid)
            return dict(entry) if entry else None

    def snapshot_meta(self) -> dict:
        """整體狀態（給 healthcheck / UI 顯示 cache age 用）。"""
        with self._lock:
            return {
                "fetch_date": self._fetch_date.isoformat() if self._fetch_date else None,
                "symbol_count": len(self._snapshot),
            }

    def batch_snapshot(self, project) -> dict:
        """全市場 snapshot；project 是 callable (info_dict) → 投影後 dict。

        持鎖期間執行 dict comprehension（4000 items × 簡單操作 < 5ms），
        不外露 _snapshot / _lock 給 API 層，維持封裝。
        """
        with self._lock:
            return {sid: project(info) for sid, info in self._snapshot.items()}


# Module-level singleton
_cache: Optional[LargeHolderCache] = None
_cache_lock = threading.Lock()


def get_cache() -> LargeHolderCache:
    """Lazy singleton。"""
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = LargeHolderCache()
        return _cache


def get_large_holder_info(symbol: str) -> Optional[dict]:
    """便利函式：取得單一 symbol 大戶資訊。"""
    return get_cache().get(symbol)


def interpret_concentration(large_pct: float) -> str:
    """根據大戶 % 給文字描述（用戶 2026-06-21 討論的門檻）。"""
    if large_pct >= 85:
        return "極度集中"
    if large_pct >= 70:
        return "集中"
    if large_pct >= 50:
        return "中等"
    return "散戶為主"
