"""VirtualBroker — 虛擬模擬填單。

VirtualBroker 不持有狀態：呼叫 submit() 直接以「要求的價量」回成交結果，
ledger 由 `SectorTradingManager` 的 `_apply_fill_to_ledger()` 統一寫入。

這讓 VirtualBroker 與 SinopacBroker 共用 *完全相同* 的 ledger-write 路徑，
也方便日後切換或同時跑多種 broker。
"""

from __future__ import annotations

import logging

from .base import Broker, BrokerResult

logger = logging.getLogger(__name__)


class VirtualBroker:
    """無狀態虛擬 broker。所有 submit 都立即「成交」。"""

    name = "virtual"

    def __init__(self) -> None:
        pass

    def submit(
        self,
        *,
        symbol: str,
        action: str,
        qty_shares: int,
        limit_price: float,
        client_order_id: str,
        sector_id: str,
        signal_desc: str = "",
    ) -> BrokerResult:
        if qty_shares <= 0 or limit_price <= 0:
            return BrokerResult(
                ok=False,
                fill_status="rejected",
                reason="invalid_qty_or_price",
            )
        logger.debug(
            "virtual fill: %s %s %s qty=%d price=%.2f cid=%s",
            sector_id, action, symbol, qty_shares, limit_price, client_order_id,
        )
        return BrokerResult(
            ok=True,
            actual_qty=qty_shares,
            actual_price=limit_price,
            order_id=client_order_id,
            fill_status="filled",
        )

    def cancel(self, order_id: str) -> bool:  # noqa: ARG002
        return True   # virtual: nothing to cancel

    def reconcile(self) -> list[BrokerResult]:
        return []     # virtual: nothing in flight

    def get_account_positions(self) -> dict[str, int]:
        return {}     # virtual: ledger is the source of truth
