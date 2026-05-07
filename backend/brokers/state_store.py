"""跨類股的 broker 控制狀態（pending_orders、cooldowns、daily_lock）。

設計重點：
- atomic write（.tmp → os.rename），避免 daemon thread + callback thread 並寫壞檔
- 全域單一 RLock，所有讀寫都必須持鎖
- 日期 key 用 Asia/Taipei 字串（YYYY-MM-DD），跟既有慣例一致
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

_TW_TZ = pytz.timezone("Asia/Taipei")


def today_tw() -> str:
    """今天的 Asia/Taipei 日期（YYYY-MM-DD）。"""
    return datetime.now(_TW_TZ).strftime("%Y-%m-%d")


@dataclass
class PendingOrder:
    client_order_id: str
    sector_id: str
    symbol: str
    action: str           # "BUY" | "SELL"
    qty_shares: int
    limit_price: float
    submitted_at: float   # unix ts
    broker_order_id: str = ""   # Shioaji 回傳的 id，submit 後填
    notes: str = ""


@dataclass
class DailyLock:
    date: str = ""
    reason: str = ""

    def active(self, today: str) -> bool:
        return bool(self.date) and self.date == today


@dataclass
class BrokerState:
    """整體控制狀態（跨類股）。"""
    pending_orders: dict[str, PendingOrder] = field(default_factory=dict)  # cid → PendingOrder
    cooldowns: dict[str, float] = field(default_factory=dict)              # f"{sector_id}:{symbol}:{action}" → expires_at
    daily_lock: DailyLock = field(default_factory=DailyLock)
    daily_orders: dict[str, dict[str, int]] = field(default_factory=dict)  # date → {sector_id|"_total" → count}
    daily_realized_pnl: dict[str, dict[str, float]] = field(default_factory=dict)  # date → {sector_id → pnl}

    def to_dict(self) -> dict:
        return {
            "pending_orders": {k: asdict(v) for k, v in self.pending_orders.items()},
            "cooldowns": dict(self.cooldowns),
            "daily_lock": asdict(self.daily_lock),
            "daily_orders": dict(self.daily_orders),
            "daily_realized_pnl": dict(self.daily_realized_pnl),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BrokerState":
        po = {
            k: PendingOrder(**v) for k, v in (data.get("pending_orders") or {}).items()
        }
        lock_raw = data.get("daily_lock") or {}
        return cls(
            pending_orders=po,
            cooldowns=dict(data.get("cooldowns") or {}),
            daily_lock=DailyLock(**lock_raw) if lock_raw else DailyLock(),
            daily_orders={d: dict(v) for d, v in (data.get("daily_orders") or {}).items()},
            daily_realized_pnl={
                d: dict(v) for d, v in (data.get("daily_realized_pnl") or {}).items()
            },
        )


class BrokerStateStore:
    """執行緒安全的 broker 狀態存取（atomic JSON）。"""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        self._state = self._load()

    # ── 持久化 ──

    def _load(self) -> BrokerState:
        if not os.path.exists(self.file_path):
            return BrokerState()
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return BrokerState.from_dict(json.load(f))
        except Exception as e:
            logger.warning(f"broker_state load failed ({e}); starting fresh")
            return BrokerState()

    def _save_locked(self) -> None:
        """必須在 self._lock 內呼叫。"""
        d = os.path.dirname(self.file_path) or "."
        # NamedTemporaryFile in same dir → os.replace is atomic on POSIX
        fd, tmp = tempfile.mkstemp(prefix=".broker_state.", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state.to_dict(), f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.file_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── pending orders ──

    def add_pending(self, p: PendingOrder) -> None:
        with self._lock:
            self._state.pending_orders[p.client_order_id] = p
            self._save_locked()

    def try_reserve_for_symbol(self, p: PendingOrder) -> bool:
        """Atomic check-and-insert：若該 (sector_id, symbol) 沒有 in-flight 訂單就插入並回 True。

        修補 TOCTOU race：原本 RiskGate.allow 與 execute_trade.add_pending 是兩次獨立鎖，
        並發呼叫可能同時通過 dedup → 重複下單。這個方法在單一鎖內完成 check + insert。
        """
        with self._lock:
            for existing in self._state.pending_orders.values():
                if existing.sector_id == p.sector_id and existing.symbol == p.symbol:
                    return False
            self._state.pending_orders[p.client_order_id] = p
            self._save_locked()
            return True

    def get_pending(self, client_order_id: str) -> Optional[PendingOrder]:
        with self._lock:
            return self._state.pending_orders.get(client_order_id)

    def has_pending_for_symbol(self, sector_id: str, symbol: str) -> bool:
        with self._lock:
            for p in self._state.pending_orders.values():
                if p.sector_id == sector_id and p.symbol == symbol:
                    return True
            return False

    def list_pending(self) -> list[PendingOrder]:
        with self._lock:
            return list(self._state.pending_orders.values())

    def remove_pending(self, client_order_id: str) -> None:
        with self._lock:
            self._state.pending_orders.pop(client_order_id, None)
            self._save_locked()

    def update_pending_broker_id(self, client_order_id: str, broker_order_id: str) -> None:
        with self._lock:
            p = self._state.pending_orders.get(client_order_id)
            if p:
                p.broker_order_id = broker_order_id
                self._save_locked()

    # ── cooldowns ──

    @staticmethod
    def _cooldown_key(sector_id: str, symbol: str, action: str) -> str:
        return f"{sector_id}:{symbol}:{action}"

    def set_cooldown(self, sector_id: str, symbol: str, action: str, expires_at: float) -> None:
        with self._lock:
            self._state.cooldowns[self._cooldown_key(sector_id, symbol, action)] = expires_at
            self._save_locked()

    def in_cooldown(self, sector_id: str, symbol: str, action: str, now: float) -> bool:
        with self._lock:
            exp = self._state.cooldowns.get(self._cooldown_key(sector_id, symbol, action), 0.0)
            return now < exp

    def cooldown_remaining(self, sector_id: str, symbol: str, action: str, now: float) -> float:
        with self._lock:
            exp = self._state.cooldowns.get(self._cooldown_key(sector_id, symbol, action), 0.0)
            return max(0.0, exp - now)

    # ── daily lock (kill-switch) ──

    def is_locked_today(self, today: str) -> bool:
        with self._lock:
            return self._state.daily_lock.active(today)

    def lock_today(self, today: str, reason: str) -> None:
        with self._lock:
            self._state.daily_lock = DailyLock(date=today, reason=reason)
            self._save_locked()
            logger.warning(f"daily kill-switch ENGAGED for {today}: {reason}")

    def get_daily_lock(self) -> DailyLock:
        with self._lock:
            # 回 copy 避免外部誤改
            return DailyLock(date=self._state.daily_lock.date, reason=self._state.daily_lock.reason)

    # ── daily counters ──

    def incr_order_count(self, today: str, sector_id: str) -> None:
        with self._lock:
            day = self._state.daily_orders.setdefault(today, {})
            day[sector_id] = day.get(sector_id, 0) + 1
            day["_total"] = day.get("_total", 0) + 1
            self._save_locked()

    def get_order_count(self, today: str, sector_id: str = "_total") -> int:
        with self._lock:
            return self._state.daily_orders.get(today, {}).get(sector_id, 0)

    def add_realized_pnl(self, today: str, sector_id: str, pnl: float) -> None:
        with self._lock:
            day = self._state.daily_realized_pnl.setdefault(today, {})
            day[sector_id] = day.get(sector_id, 0.0) + pnl
            day["_total"] = day.get("_total", 0.0) + pnl
            self._save_locked()

    def get_realized_pnl(self, today: str, sector_id: str = "_total") -> float:
        with self._lock:
            return self._state.daily_realized_pnl.get(today, {}).get(sector_id, 0.0)
