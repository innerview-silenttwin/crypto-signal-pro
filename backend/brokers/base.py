"""Broker 抽象介面與資料類別。

所有 broker 實作都遵循同一個介面，`SectorTradingManager` 只認 Broker、不認哪一家券商。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


Action = Literal["BUY", "SELL"]
FillStatus = Literal["filled", "partial", "rejected", "cancelled", "timeout", "skipped"]


@dataclass
class BrokerResult:
    """下單結果。失敗時 ok=False，呼叫端不寫 ledger。"""

    ok: bool
    actual_qty: int = 0           # 股（不是張）
    actual_price: float = 0.0     # 元/股
    order_id: str = ""            # 券商回的訂單 id（虛擬模式為 client_order_id）
    fill_status: FillStatus = "rejected"
    reason: str = ""              # 失敗或 skipped 原因（給 log/Telegram）


@dataclass
class RiskDecision:
    """RiskGate 對單筆委託的決策。"""

    ok: bool
    reason: str = ""
    # 給 skipped 通知用的補充欄位（未通過時填）
    needed_twd: float = 0.0
    available_twd: float = 0.0
    extra: dict = field(default_factory=dict)


@runtime_checkable
class Broker(Protocol):
    """Broker 介面。實作必須是 thread-safe（會在 daemon thread 與 callback thread 並存）。"""

    name: str

    def submit(
        self,
        *,
        symbol: str,
        action: Action,
        qty_shares: int,
        limit_price: float,
        client_order_id: str,
        sector_id: str,
        signal_desc: str,
    ) -> BrokerResult:
        """送出限價單；阻塞直到成交、被拒、或 timeout。"""
        ...

    def cancel(self, order_id: str) -> bool: ...

    def reconcile(self) -> list[BrokerResult]:
        """同步 in-flight 訂單最新狀態。回傳已完成（成交/取消/拒絕）的訂單，呼叫端負責寫 ledger。"""
        ...

    def get_account_positions(self) -> dict[str, int]:
        """券商側真實持倉（symbol → 股數）。VirtualBroker 回傳本地 ledger 的持倉。"""
        ...
