"""SinopacBroker — 永豐 Shioaji Python API 包裝。

此模組只實作 v1（Phase 1）必要的功能：
  - simulation=True 登入、不啟用 CA
  - 整股 (Common) + 現股 (Cash) + 限價 (LMT) + ROD
  - 同步 poll-until-filled（30 秒 timeout）
  - reconcile：把 in-flight 訂單同步狀態
  - 失敗 fallback（連線/登入/下單拋例外時，呼叫端會回 ok=False）

明確不做（v2 議題）：
  - activate_ca（CA 啟用）
  - market（市價）/ IntradayOdd / Margin / Short / DayTradeShort
  - 行情訂閱（v1 沿用 yfinance）

安全注意：
  - 禁止把 self.api、credentials、env 物件 log 出去
  - shioaji 內建 logger 直接停用，避免它寫進 ~/log/shioaji.log
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .base import BrokerResult
from .state_store import BrokerStateStore

logger = logging.getLogger(__name__)

# 預設 poll 與 timeout（建構子可覆寫）
DEFAULT_POLL_INTERVAL_S = 0.5
DEFAULT_FILL_TIMEOUT_S = 30


class SinopacBroker:
    """永豐 Shioaji 整股 broker（v1：simulation only）。

    Thread-safe：所有對 shioaji api 的呼叫都加同一把 RLock。
    """

    name = "sinopac"

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        person_id: str,
        simulation: bool = True,
        ca_path: Optional[str] = None,
        ca_password: Optional[str] = None,
        state_store: Optional[BrokerStateStore] = None,
        fill_timeout_s: int = DEFAULT_FILL_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ):
        # 將 shioaji 自家 logger 降到 WARNING，避免 INFO 帶到內部欄位
        logging.getLogger("shioaji").setLevel(logging.WARNING)

        try:
            import shioaji as sj
        except ImportError as e:
            raise RuntimeError(
                "shioaji 未安裝；prod 機請執行 pip install shioaji"
            ) from e

        self._sj = sj
        self._lock = threading.RLock()
        self._simulation = bool(simulation)
        self._fill_timeout_s = max(5, int(fill_timeout_s))
        self._poll_interval_s = max(0.05, float(poll_interval_s))
        self._state_store = state_store
        self._person_id = person_id   # 給某些 API 需要時用；不寫入 log

        self.api = sj.Shioaji(simulation=self._simulation)
        try:
            self.api.login(api_key=api_key, secret_key=secret_key)
        except Exception as e:
            # 不要把 e 的完整 repr log 出來（可能含 api_key 殘留）
            logger.error("Shioaji login failed: %s", e.__class__.__name__)
            raise

        if not self._simulation:
            if not (ca_path and ca_password):
                raise RuntimeError("非 simulation 模式必須提供 CA path + password")
            try:
                self.api.activate_ca(
                    ca_path=ca_path, ca_passwd=ca_password, person_id=person_id
                )
            except Exception as e:
                logger.error("Shioaji activate_ca failed: %s", e.__class__.__name__)
                raise

        # Order callback：保留掛勾（即時更新 pending），但 v1 我們也 poll status 主動同步
        try:
            self.api.set_order_callback(self._on_order_event)
        except Exception:
            # 老版本 API 沒有 set_order_callback 也不擋
            logger.debug("set_order_callback unavailable; relying on polling")

        # in-flight: client_order_id → Trade 物件（shioaji 用）
        self._in_flight: dict[str, object] = {}

        # place_order 統計 + 連續失敗計數（in-memory；process 重啟會 reset）
        # 用於：health_check 顯示、連續 N 次失敗發 Telegram 警告
        self._stats_lock = threading.Lock()
        self._stats = {
            "place_order_success": 0,        # 一發即成功
            "place_order_retry_success": 0,  # retry 後成功
            "place_order_failed": 0,         # retry 後仍失敗
        }
        self._consecutive_failures = 0       # 全 sector 連續失敗計數
        self._last_failure_alert_at = 0.0    # 上次發警告時間（避免洗版）

        logger.info(
            "SinopacBroker ready (simulation=%s, fill_timeout=%ds)",
            self._simulation, self._fill_timeout_s,
        )

    # ── 統計 (給 health_check / 外部觀察) ──
    def get_stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats, consecutive_failures=self._consecutive_failures)

    def _place_order_with_retry(self, contract, order, max_retries: int = 1,
                                 retry_sleep_s: float = 2.0):
        """包裝 api.place_order — TimeoutError 自動 retry 1 次。

        為什麼要 retry？
          - Solace MQ session 偶爾會掉到 "Not ready" 狀態（永豐 simulation 主機側問題）
          - 此時 place_order 會丟 TimeoutError
          - 通常 2 秒後 Solace 自動重連，再試一次往往成功

        Args:
            max_retries: 額外重試次數（不含第一次）。預設 1 → 最多 2 次嘗試
            retry_sleep_s: 失敗後等待秒數，讓 Solace 重連

        Returns:
            (trade, retry_used: bool) — retry_used 表示是否用到 retry 才成功

        Raises:
            最後一次 attempt 的例外（不吞）
        """
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                trade = self.api.place_order(contract, order)
                return trade, (attempt > 0)
            except Exception as e:
                last_err = e
                err_name = e.__class__.__name__
                # 只有 timeout 類錯誤才重試；其他直接 raise
                if "Timeout" not in err_name:
                    raise
                if attempt < max_retries:
                    logger.warning(
                        "place_order timeout (attempt %d/%d), sleep %.1fs then retry",
                        attempt + 1, max_retries + 1, retry_sleep_s,
                    )
                    time.sleep(retry_sleep_s)
                else:
                    logger.error(
                        "place_order timeout after %d attempts",
                        max_retries + 1,
                    )
        # 不會到這（finally raise），保險
        raise last_err if last_err else RuntimeError("unknown place_order failure")

    def _on_place_order_success(self, retry_used: bool) -> None:
        """成功時更新統計、重置連續失敗計數。"""
        with self._stats_lock:
            if retry_used:
                self._stats["place_order_retry_success"] += 1
            else:
                self._stats["place_order_success"] += 1
            self._consecutive_failures = 0

    def _on_place_order_failure(self) -> None:
        """失敗時更新統計、累計連續失敗；連續 ≥ 3 次發 Telegram 警告（每 10 分鐘最多一次）。"""
        with self._stats_lock:
            self._stats["place_order_failed"] += 1
            self._consecutive_failures += 1
            count = self._consecutive_failures
            now = time.time()
            should_alert = (count >= 3 and (now - self._last_failure_alert_at) > 600)
            if should_alert:
                self._last_failure_alert_at = now
        if should_alert:
            try:
                # lazy import 避免 broker 模組依賴 notifier
                from notifier import send_telegram
                send_telegram(
                    f"🚨 <b>Sinopac 連線異常</b>\n"
                    f"place_order 連續失敗 {count} 次\n"
                    f"可能是 Solace session 掛了 / 永豐 simulation 主機問題\n"
                    f"系統會持續重試；若仍持續發生請手動 csp restart"
                )
            except Exception as e:
                logger.warning("Telegram alert failed: %s", e.__class__.__name__)

    # ── callback（safe to be called from another thread）──

    def _on_order_event(self, *args, **kwargs):  # noqa: ARG002
        """Shioaji order callback。我們用 poll 為主，這裡只 debug log。"""
        try:
            logger.debug("order_event received")
        except Exception:
            pass

    # ── Broker 介面 ──

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
        # 股數至少 1 股；< 1000 走盤中零股 (IntradayOdd)，>= 1000 走整股 (Common)
        # 不做混合（>=1000 + 餘數零股），實際 qty_shares 帶整千的部分過去；零股走 odd
        # TODO(Phase 2 真錢): 真錢上線前，考慮把這條改回 ≥10，避免不經濟的 dust 交易
        if qty_shares < 1:
            return BrokerResult(
                ok=False,
                fill_status="rejected",
                reason=f"below_min_lot_qty={qty_shares}",
            )

        is_odd_lot = qty_shares < 1000
        # IntradayOdd 用「股」，Common 用「張」
        sj_quantity = qty_shares if is_odd_lot else (qty_shares // 1000)

        contract = self._resolve_contract(symbol)
        if contract is None:
            return BrokerResult(
                ok=False,
                fill_status="rejected",
                reason=f"contract_not_found:{symbol}",
            )

        sj = self._sj
        try:
            order = self.api.Order(
                action=sj.constant.Action.Buy if action == "BUY" else sj.constant.Action.Sell,
                price=float(limit_price),
                quantity=int(sj_quantity),
                price_type=sj.constant.StockPriceType.LMT,
                order_type=sj.constant.OrderType.ROD,
                order_lot=(sj.constant.StockOrderLot.IntradayOdd if is_odd_lot
                          else sj.constant.StockOrderLot.Common),
                order_cond=sj.constant.StockOrderCond.Cash,
                account=self.api.stock_account,
                # daytrade_short 留預設（False）
            )
        except Exception as e:
            logger.error("Shioaji Order construction failed: %s", e.__class__.__name__)
            return BrokerResult(ok=False, fill_status="rejected", reason="order_build_error")

        with self._lock:
            try:
                trade, retry_used = self._place_order_with_retry(contract, order)
            except Exception as e:
                logger.error("place_order failed: %s", e.__class__.__name__)
                self._on_place_order_failure()
                return BrokerResult(ok=False, fill_status="rejected", reason="place_order_error")
            self._on_place_order_success(retry_used)
            self._in_flight[client_order_id] = trade

        # 取得 broker order id（避免 callback 競態：盡量早記錄）
        broker_order_id = self._safe_order_id(trade)
        if self._state_store is not None and broker_order_id:
            self._state_store.update_pending_broker_id(client_order_id, broker_order_id)

        # poll 直到 filled / partfilled / cancelled / rejected / timeout
        deadline = time.time() + self._fill_timeout_s
        last_status = ""
        while time.time() < deadline:
            with self._lock:
                self._safe_update_status()
                status = self._read_trade_status(trade)
            last_status = status.get("status", "")

            # Filled（完全成交）→ 直接回
            if status.get("filled"):
                with self._lock:
                    self._in_flight.pop(client_order_id, None)
                # 零股：deal_quantity 已是「股」；整股：deal_quantity 是「張」
                deal_qty = status.get("deal_quantity") or sj_quantity
                actual_qty_shares = int(deal_qty) if is_odd_lot else int(deal_qty) * 1000
                actual_price = float(status.get("avg_price") or limit_price)
                return BrokerResult(
                    ok=True,
                    actual_qty=actual_qty_shares,
                    actual_price=actual_price,
                    order_id=broker_order_id,
                    fill_status="filled",
                )

            # PartFilled：等到 deadline 仍 partfilled 才算 partial（成交一張 vs 還在補單）
            # 中途 partfilled 不立即回，繼續等可能轉 filled

            if status.get("rejected") or status.get("cancelled"):
                with self._lock:
                    self._in_flight.pop(client_order_id, None)
                return BrokerResult(
                    ok=False,
                    fill_status=("rejected" if status.get("rejected") else "cancelled"),
                    reason=last_status,
                    order_id=broker_order_id,
                )
            time.sleep(self._poll_interval_s)

        # Timeout：先看是否有 partial fill 可保留
        with self._lock:
            self._safe_update_status()
            final_status = self._read_trade_status(trade)
        deal_qty = final_status.get("deal_quantity") or 0
        if deal_qty > 0:
            # 部分成交：取消剩餘 + 回報 partial（套 timeout 防止 hang）
            self._safe_cancel(trade, client_order_id, label="partial")
            actual_price = float(final_status.get("avg_price") or limit_price)
            return BrokerResult(
                ok=True,
                actual_qty=int(deal_qty) if is_odd_lot else int(deal_qty) * 1000,
                actual_price=actual_price,
                order_id=broker_order_id,
                fill_status="partial",
            )

        # timeout → cancel（套 timeout 防止 Solace 不穩時永久 hang）
        self._safe_cancel(trade, client_order_id, label="timeout")
        return BrokerResult(
            ok=False,
            fill_status="timeout",
            reason=f"unfilled_after_{self._fill_timeout_s}s_status={last_status}",
            order_id=broker_order_id,
        )

    def cancel(self, order_id: str) -> bool:
        # v1：透過 client_order_id 找 Trade；對齊 in-flight map
        with self._lock:
            trade = self._in_flight.get(order_id)
            if trade is None:
                return False
            try:
                self.api.cancel_order(trade)
                return True
            except Exception as e:
                logger.warning("cancel_order failed: %s", e.__class__.__name__)
                return False

    def reconcile(self) -> list[BrokerResult]:
        """同步 in-flight 訂單狀態。回傳已完成（filled/rejected/cancelled）的 BrokerResult 給呼叫端。

        注意：因為 v1 的 submit() 是同步等成交，正常情況 reconcile 只會在「重啟後 in-flight 殘留」時起作用。
        """
        completed: list[BrokerResult] = []
        with self._lock:
            try:
                self.api.update_status()
            except Exception as e:
                logger.debug("update_status in reconcile: %s", e.__class__.__name__)
                return completed
            in_flight_snapshot = list(self._in_flight.items())

        for cid, trade in in_flight_snapshot:
            status = self._read_trade_status(trade)
            if status.get("filled"):
                deal_qty = status.get("deal_quantity") or 0
                trade_is_odd = self._is_trade_odd_lot(trade)
                completed.append(BrokerResult(
                    ok=True,
                    actual_qty=int(deal_qty) if trade_is_odd else int(deal_qty) * 1000,
                    actual_price=float(status.get("avg_price") or 0.0),
                    order_id=self._safe_order_id(trade),
                    fill_status="filled",
                ))
                with self._lock:
                    self._in_flight.pop(cid, None)
            elif status.get("rejected") or status.get("cancelled"):
                completed.append(BrokerResult(
                    ok=False,
                    fill_status=("rejected" if status.get("rejected") else "cancelled"),
                    order_id=self._safe_order_id(trade),
                    reason=status.get("status", ""),
                ))
                with self._lock:
                    self._in_flight.pop(cid, None)
        return completed

    def get_account_positions(self) -> dict[str, int]:
        """券商側真實持倉（symbol → 股數）。失敗回空 dict（呼叫端僅用於對帳警示）。"""
        try:
            with self._lock:
                positions = self.api.list_positions(self.api.stock_account)
        except Exception as e:
            logger.warning("list_positions failed: %s", e.__class__.__name__)
            return {}

        out: dict[str, int] = {}
        for p in positions or []:
            try:
                code = getattr(p, "code", None) or getattr(p, "symbol", None)
                qty_lots = getattr(p, "quantity", 0) or 0
                if code:
                    sym = code if code.endswith(".TW") else f"{code}.TW"
                    out[sym] = int(qty_lots) * 1000
            except Exception:
                continue
        return out

    # ── 內部工具 ──

    def _is_trade_odd_lot(self, trade) -> bool:
        """從 trade.order.order_lot 回讀此訂單是不是盤中零股，給 reconcile 後正確換算成股。"""
        try:
            ol = trade.order.order_lot
            return ol == self._sj.constant.StockOrderLot.IntradayOdd
        except Exception:
            return False

    def _safe_cancel(self, trade, client_order_id: str, *, label: str,
                     timeout_s: float = 5.0) -> bool:
        """套 timeout 包裝 api.cancel_order，避免 Solace 不穩時 cancel 永久 hang。

        Args:
            label: 給 log 辨識的標籤（"timeout" / "partial" / "external"）
            timeout_s: cancel 最多等多久；超過就放棄等待（thread 仍在背景跑）

        Returns:
            True 表示 cancel 成功且 in-flight 已清；False 表示掛掉或 timeout
        """
        result = {"ok": False, "err": None}

        def _do_cancel():
            try:
                with self._lock:
                    self.api.cancel_order(trade)
                    self._in_flight.pop(client_order_id, None)
                result["ok"] = True
            except Exception as e:
                result["err"] = e.__class__.__name__

        t = threading.Thread(target=_do_cancel, daemon=True,
                             name=f"sinopac-cancel-{label}-{client_order_id[:8]}")
        t.start()
        t.join(timeout=timeout_s)
        if t.is_alive():
            # cancel 仍在等回應 → 不再等，留在背景跑（會 leak thread 但不會 leak pending）
            logger.warning(
                "cancel after %s timed out (>%ds), abandoning to background",
                label, int(timeout_s),
            )
            # 主動把 in-flight 清掉（即使 cancel 沒成功，至少我們系統不再追蹤）
            with self._lock:
                self._in_flight.pop(client_order_id, None)
            return False
        if result["err"]:
            logger.warning("cancel after %s failed: %s", label, result["err"])
            return False
        return True

    def _resolve_contract(self, symbol: str):
        """symbol 形如 '2330.TW'；取出 4 碼代號，找 self.api.Contracts.Stocks."""
        code = symbol.split(".")[0]
        try:
            with self._lock:
                # 大多版本：Contracts.Stocks[code]
                return self.api.Contracts.Stocks[code]
        except (KeyError, AttributeError):
            try:
                with self._lock:
                    return getattr(self.api.Contracts.Stocks, code)
            except Exception as e:
                logger.warning(
                    "contract lookup failed: %s (%s)", code, e.__class__.__name__
                )
                return None

    def _safe_update_status(self) -> None:
        """同步訂單狀態。不同 shioaji 版本可能要求 (account=) 參數，做 fallback。

        必須在 self._lock 內呼叫。
        """
        try:
            self.api.update_status()
            return
        except TypeError:
            # 舊版可能必須傳 account
            try:
                self.api.update_status(self.api.stock_account)
                return
            except Exception as e:
                logger.debug("update_status(account) failed: %s", e.__class__.__name__)
        except Exception as e:
            logger.debug("update_status failed: %s", e.__class__.__name__)

    @staticmethod
    def _read_trade_status(trade) -> dict:
        """從 shioaji Trade 物件抽取我們在意的欄位。

        OrderState 列舉值（依官方 llms-full.txt）：
          Inactive / Submitted / Filled / Cancelled / PartFilled / Failed

        注意：`PartFilled` 也包含 "filled" 字串，因此**必須先判 partfilled 再判 filled**。

        成交價量取得：trade.status 沒有直接的 deal_quantity / avg_price 欄位，
        而是放在 status.deals (list[Deal])，每個 Deal 有 price 與 quantity。
        我們對 deals 加總算出 actual_qty（張）與成交均價。
        """
        try:
            status = trade.status
            status_value = getattr(status, "status", "") or ""
            s_str = str(status_value).lower()

            # 先抓 partfilled，再抓 filled（避免 PartFilled 被誤判為 Filled）
            is_part = ("partfilled" in s_str)
            is_filled = ("filled" in s_str) and not is_part
            is_cancelled = ("cancelled" in s_str or "canceled" in s_str)
            is_rejected = ("rejected" in s_str or "failed" in s_str)

            # 從 deals 算 actual_qty（張）與成交均價
            deals = getattr(status, "deals", None) or []
            total_qty_lots = 0
            sum_qty_price = 0.0
            for d in deals:
                try:
                    q = int(getattr(d, "quantity", 0) or 0)
                    p = float(getattr(d, "price", 0.0) or 0.0)
                    total_qty_lots += q
                    sum_qty_price += q * p
                except Exception:
                    continue
            avg_price = (sum_qty_price / total_qty_lots) if total_qty_lots > 0 else None
            # 若 deals 抓不到（極舊版本），保留 modified_price 當 fallback
            if avg_price is None:
                modified = getattr(status, "modified_price", 0)
                avg_price = float(modified) if modified else None

            return {
                "status": s_str,
                "filled": is_filled,
                "partfilled": is_part,
                "cancelled": is_cancelled,
                "rejected": is_rejected,
                "deal_quantity": total_qty_lots,
                "avg_price": avg_price,
            }
        except Exception:
            return {
                "status": "unknown", "filled": False, "partfilled": False,
                "cancelled": False, "rejected": False,
                "deal_quantity": 0, "avg_price": None,
            }

    @staticmethod
    def _safe_order_id(trade) -> str:
        for attr in ("order_id", "id", "ordno"):
            try:
                v = getattr(trade.order, attr, None) or getattr(trade.status, attr, None)
                if v:
                    return str(v)
            except Exception:
                continue
        return ""
