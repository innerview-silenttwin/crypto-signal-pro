"""風險閘 — 在 broker.submit() 前最後一道檢查。

規則順序固定（前面的失敗就直接擋）：
  1. 市場時段（is_orderable_now / 非節假日）
  2. 全域 daily_lock（kill-switch）
  3. cooldown（同方向同 symbol 冷卻中）
  4. pending_orders 衝突（同 symbol 已有未成交單）
  5. BUY-專屬：張數 ≥ 1、單筆金額上限、每日筆數上限、持倉占比上限
  6. SELL-專屬：除權息凍結（自動停損用）、本地有持倉

所有規則閥值由 broker_config.yaml 提供，可在跑動時 reload。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .base import RiskDecision
from .market_hours import (
    is_holiday,
    is_orderable_now,
    is_within_ex_div_freeze,
    now_tw,
)
from .state_store import BrokerStateStore, today_tw

logger = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    """風控閾值。所有金額單位為新台幣（TWD）。"""

    max_order_amount_twd: float = 100_000.0
    min_order_amount_twd: float = 20_000.0   # 低於此金額視為「銀彈不足」，發通知不下單
    max_daily_orders_total: int = 20
    max_daily_orders_per_sector: int = 10
    max_daily_loss_pct: float = 2.0          # 觸發 kill-switch 的當日累計虧損百分比（vs initial_balance）
    max_position_pct_per_symbol: float = 15.0
    cooldown_minutes_after_sell: float = 60.0    # SELL 完幾分鐘內不允許 BUY 同檔
    cooldown_minutes_after_buy: float = 30.0     # BUY 完幾分鐘內不允許再 BUY 同檔（非 SELL）
    enforce_market_hours: bool = True             # 測試或盤後重放可關


class RiskGate:
    """單例（每進程一個），由 sector_auto_trader 啟動時建立。"""

    def __init__(
        self,
        config: RiskConfig,
        state_store: BrokerStateStore,
        *,
        equity_provider: Callable[[str], tuple[float, float]],
        position_provider: Callable[[str, str], dict],
        initial_balance_provider: Callable[[str], float],
        clock: Callable[[], float] = time.time,
    ):
        """
        Args:
            equity_provider: sector_id → (equity_now_twd, balance_now_twd)
            position_provider: (sector_id, symbol) → {"qty": int (股), "avg_price": float}
            initial_balance_provider: sector_id → initial_balance_twd（給 kill-switch 用）
            clock: 注入測試時鐘
        """
        self.cfg = config
        self.store = state_store
        self.equity_provider = equity_provider
        self.position_provider = position_provider
        self.initial_balance_provider = initial_balance_provider
        self.clock = clock

    # ── 主決策 ──

    def allow(
        self,
        *,
        sector_id: str,
        symbol: str,
        action: str,                 # "BUY" | "SELL"
        qty_shares: int,
        limit_price: float,
        is_auto_stop: bool = False,  # True 代表「停損/停利自動觸發」
    ) -> RiskDecision:
        today = today_tw()

        # 1. 市場時段
        if self.cfg.enforce_market_hours:
            if not is_orderable_now():
                return RiskDecision(ok=False, reason="market_closed")
            if is_holiday(today):
                return RiskDecision(ok=False, reason="holiday")

        # 2. kill-switch
        if self.store.is_locked_today(today):
            lock = self.store.get_daily_lock()
            return RiskDecision(ok=False, reason=f"daily_locked:{lock.reason}")

        # 3. cooldown
        now_ts = self.clock()
        # BUY 同檔：先看「BUY 後 30 分」與「SELL 後 60 分」兩條
        if action == "BUY":
            if self.store.in_cooldown(sector_id, symbol, "BUY", now_ts):
                rem = self.store.cooldown_remaining(sector_id, symbol, "BUY", now_ts)
                return RiskDecision(ok=False, reason=f"cooldown_buy:{int(rem)}s")
            if self.store.in_cooldown(sector_id, symbol, "SELL", now_ts):
                rem = self.store.cooldown_remaining(sector_id, symbol, "SELL", now_ts)
                return RiskDecision(ok=False, reason=f"cooldown_after_sell:{int(rem)}s")

        # 4. pending order 衝突
        if self.store.has_pending_for_symbol(sector_id, symbol):
            return RiskDecision(ok=False, reason="pending_exists")

        # 5/6. action-specific
        if action == "BUY":
            return self._check_buy(sector_id, symbol, qty_shares, limit_price, today)
        elif action == "SELL":
            return self._check_sell(sector_id, symbol, today, is_auto_stop, qty_shares)
        else:
            return RiskDecision(ok=False, reason=f"unknown_action:{action}")

    def _check_buy(
        self,
        sector_id: str,
        symbol: str,
        qty_shares: int,
        limit_price: float,
        today: str,
    ) -> RiskDecision:
        # 5a. 股數下限：允許零股，但至少 10 股；買不到 10 股才擋
        if qty_shares < 10:
            equity, balance = self.equity_provider(sector_id)
            needed = 10 * limit_price * 1.001425  # 買 10 股的成本（含手續費）
            return RiskDecision(
                ok=False,
                reason="below_min_lot",
                needed_twd=needed,
                available_twd=balance,
                extra={"qty_shares_requested": qty_shares, "limit_price": limit_price},
            )

        order_amount = qty_shares * limit_price

        # 5b. 單筆金額上限
        if order_amount > self.cfg.max_order_amount_twd:
            return RiskDecision(
                ok=False,
                reason=f"over_max_order:{order_amount:.0f}>{self.cfg.max_order_amount_twd:.0f}",
                needed_twd=order_amount,
                available_twd=self.cfg.max_order_amount_twd,
            )

        # 5b'. 單筆金額下限（銀彈不足）— 算出來的 qty * price 太低代表帳戶餘額不足
        # 注意：只在 min_order_amount_twd > 0 時生效；設 0 可關掉
        if self.cfg.min_order_amount_twd > 0 and order_amount < self.cfg.min_order_amount_twd:
            equity, balance = self.equity_provider(sector_id)
            return RiskDecision(
                ok=False,
                reason=f"below_min_order_amount:{order_amount:.0f}<{self.cfg.min_order_amount_twd:.0f}",
                needed_twd=self.cfg.min_order_amount_twd,
                available_twd=balance,
                extra={"qty_shares_requested": qty_shares, "limit_price": limit_price},
            )

        # 5c. 每日筆數上限
        if self.store.get_order_count(today, "_total") >= self.cfg.max_daily_orders_total:
            return RiskDecision(ok=False, reason="max_daily_orders_total")
        if self.store.get_order_count(today, sector_id) >= self.cfg.max_daily_orders_per_sector:
            return RiskDecision(ok=False, reason="max_daily_orders_per_sector")

        # 5d. 持倉占比上限（含本筆）
        equity, _ = self.equity_provider(sector_id)
        if equity <= 0:
            return RiskDecision(ok=False, reason="equity_zero")
        existing = self.position_provider(sector_id, symbol)
        existing_value = (existing.get("qty", 0) or 0) * (existing.get("avg_price", 0.0) or 0.0)
        new_pos_pct = (existing_value + order_amount) / equity * 100
        if new_pos_pct > self.cfg.max_position_pct_per_symbol:
            return RiskDecision(
                ok=False,
                reason=f"over_position_pct:{new_pos_pct:.1f}>{self.cfg.max_position_pct_per_symbol}",
            )

        return RiskDecision(ok=True)

    def _check_sell(
        self,
        sector_id: str,
        symbol: str,
        today: str,
        is_auto_stop: bool,
        qty_shares: int = 0,
    ) -> RiskDecision:
        hold = self.position_provider(sector_id, symbol)
        if not hold or (hold.get("qty", 0) or 0) <= 0:
            return RiskDecision(ok=False, reason="no_position")

        # Dust position：持倉 < 10 股，永豐不收（< 整股最低門檻）
        # 這種尾數通常來自過去零股部分成交留下的渣，需要手動處理
        current_qty = qty_shares if qty_shares > 0 else (hold.get("qty", 0) or 0)
        if current_qty < 10:
            return RiskDecision(
                ok=False,
                reason=f"dust_position_qty={current_qty}",
                extra={"qty_shares_requested": current_qty},
            )

        # 除權息凍結：只擋自動停損/停利；標準信號賣 / 趨勢破壞型賣不擋
        if is_auto_stop and is_within_ex_div_freeze(symbol, today):
            return RiskDecision(ok=False, reason="ex_div_freeze")

        return RiskDecision(ok=True)

    # ── 成功後設置 cooldown / 計數 ──

    def record_success(self, sector_id: str, symbol: str, action: str) -> None:
        now_ts = self.clock()
        if action == "BUY":
            exp = now_ts + self.cfg.cooldown_minutes_after_buy * 60
            self.store.set_cooldown(sector_id, symbol, "BUY", exp)
        elif action == "SELL":
            exp = now_ts + self.cfg.cooldown_minutes_after_sell * 60
            self.store.set_cooldown(sector_id, symbol, "SELL", exp)
        self.store.incr_order_count(today_tw(), sector_id)

    # ── 觸發 kill-switch ──

    def maybe_trigger_kill_switch(self, sector_id: str, realized_pnl_delta: float) -> None:
        """SELL 結算後呼叫。累計當日 realized PnL；若超過 max_daily_loss_pct 觸發全局鎖。"""
        today = today_tw()
        self.store.add_realized_pnl(today, sector_id, realized_pnl_delta)

        # 用「全部 sector 加總實現損益」對「全部 sector initial_balance 加總」算虧損百分比
        # 但 initial_balance_provider 只接受單一 sector_id；呼叫端可選 monitor 模式
        # 這裡對單一 sector 自身做檢查，避免跨 sector 假設
        sector_initial = self.initial_balance_provider(sector_id)
        if sector_initial <= 0:
            return
        sector_realized = self.store.get_realized_pnl(today, sector_id)
        loss_pct = -sector_realized / sector_initial * 100  # 虧損為正
        if loss_pct >= self.cfg.max_daily_loss_pct:
            self.store.lock_today(
                today,
                f"sector {sector_id} daily loss {loss_pct:.2f}% >= {self.cfg.max_daily_loss_pct:.2f}%",
            )
