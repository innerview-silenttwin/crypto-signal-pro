"""處置股 (disposition stock) 偵測與預收券 (reserve_stock) 流程。

背景（2026-06-09/06-11 永豐客服澄清）：
- 台股「處置股」期間（通常 10 個交易日）採「分盤管制撮合」+「預收券款」
- 賣處置股必須先 api.reserve_stock(contract, share) 圈存庫存才能 SELL
- ⚠️ sim 環境的 reserve_stock 是 no-op（永遠回 status=False / 空），所以
  sim 必失敗；prod 真實圈存
- 2026-06-09 ~ 11 我們系統踩到 6770.TW（處置期 06/02 ~ 06/15）SELL 84 次全 fail

整合點：backend/brokers/sinopac.py::submit() 在 place_order 前呼叫
ensure_sellable()，回 False 就 skip 不送單。

詳見 memory/project_sinopac_punish_handling.md。
"""

from __future__ import annotations

import datetime
import logging
import threading
from typing import Optional, Set

logger = logging.getLogger(__name__)


# 預收券 API 服務時段（永豐客服 2026-06-11 確認）
RESERVE_START = datetime.time(8, 0)
RESERVE_END = datetime.time(14, 30)


class DispositionGuard:
    """處置股清單快取 + 賣出前預收券檢查。

    Thread-safe（cache 操作有 lock）。
    每日首次 query 自動重新拉清單（用日期當 cache key）。
    若 punish() 失敗，沿用舊清單並標 ok=False（prod 上線時可用此旗標保守擋單）。
    """

    def __init__(self, simulation: bool):
        self._simulation = bool(simulation)
        self._lock = threading.Lock()
        self._date: Optional[datetime.date] = None
        self._codes: Set[str] = set()
        self._ok = False  # punish() 是否曾成功載入；prod 開機保險用
        self._last_telegram_date: Optional[datetime.date] = None

    @staticmethod
    def _normalize(symbol: str) -> str:
        """去掉 .TW / .TWO 後綴；punish() 回的 code 是純數字。"""
        return symbol.split(".")[0]

    def get_disposition_set(self, api) -> Set[str]:
        """回傳今日處置股 code set（純數字、無 .TW）；每日 lazy refresh。

        Race-safety：整段 refresh 持鎖（含 api.punish() 網路呼叫）。punish() 是每日
        一次 + 鎖內快取命中後立刻 return，鎖的 critical section 實際上很短。
        若不鎖整段，多執行緒在 09:00 開盤首筆 burst 可能同時穿過 date check 各自呼叫
        punish() 浪費連線 + 重複 log。
        """
        today = datetime.date.today()
        with self._lock:
            # double-checked：在鎖內再次確認 cache 已是今日
            if self._date == today:
                return self._codes.copy()

            # 跨日或首次：在鎖內拉新清單
            try:
                p = api.punish()
                codes = set(getattr(p, "code", []) or [])
                self._date = today
                self._codes = codes
                self._ok = True
                logger.info("處置股清單更新 %s，共 %d 檔: %s",
                            today, len(codes), sorted(codes))
                return codes.copy()
            except Exception as e:
                self._ok = False
                logger.error("punish() 失敗，沿用舊清單(%s): %r", self._date, e)
                return self._codes.copy()

    def is_disposed(self, api, symbol: str) -> bool:
        """symbol 是否為處置股（會 lazy refresh 清單）。"""
        code = self._normalize(symbol)
        return code in self.get_disposition_set(api)

    @staticmethod
    def _in_reserve_hours(now: Optional[datetime.datetime] = None) -> bool:
        """是否在預收服務時段 08:00 ~ 14:30（不檢查交易日，呼叫端自己控）。"""
        t = (now or datetime.datetime.now()).time()
        return RESERVE_START <= t <= RESERVE_END

    def ensure_sellable(
        self,
        api,
        symbol: str,
        qty_shares: int,
        *,
        now: Optional[datetime.datetime] = None,
    ) -> tuple[bool, str]:
        """賣出處置股前的 gate。

        Args:
            symbol: 含 .TW 後綴或純數字皆可（內部正規化）
            qty_shares: 股數（不是張！reserve_stock 用股數）
            now: 用於測試注入時間，預設 datetime.now()

        Returns:
            (ok, reason)
              ok=True  → 可以送 place_order
              ok=False → reason 用於 log / skipped_trades 紀錄
        """
        code = self._normalize(symbol)

        # 非處置股 → 直接放行
        if code not in self.get_disposition_set(api):
            return True, ""

        # sim：reserve_stock 是 no-op，直接 skip（送 SELL 必失敗，6770 案例驗證）
        if self._simulation:
            return False, f"disposition_sim_noop:{code}"

        # prod：清單若從沒成功載入 → 保守擋掉（客服建議）
        with self._lock:
            disp_ok = self._ok
        if not disp_ok:
            return False, f"disposition_list_not_loaded:{code}"

        # prod：必須在服務時間內
        if not self._in_reserve_hours(now):
            return False, f"disposition_out_of_hours:{code}"

        # prod 真送預收
        try:
            contract = api.Contracts.Stocks[code]
            resp = api.reserve_stock(contract, qty_shares, account=api.stock_account)
            status = getattr(getattr(resp, "response", None), "status", False)
            if status:
                logger.info("[PROD] %s 預收券成功 share=%d", code, qty_shares)
                return True, ""
            info = getattr(getattr(resp, "response", None), "info", "")
            logger.error("[PROD] %s 預收券失敗 info=%r", code, info)
            return False, f"reserve_stock_status_false:{code}"
        except Exception as e:
            logger.exception("[PROD] %s 預收券例外: %r", code, e)
            return False, f"reserve_stock_exception:{code}:{e.__class__.__name__}"

    # ── 對外查詢（給 UI / telegram 用）──

    def snapshot(self) -> dict:
        """回 cache 當前狀態（不觸發 refresh）。給 UI / telegram 顯示用。"""
        with self._lock:
            return {
                "date": self._date.isoformat() if self._date else None,
                "ok": self._ok,
                "count": len(self._codes),
                "codes": sorted(self._codes),
            }

    def should_send_daily_telegram(self) -> bool:
        """每日只該發一次處置股摘要 telegram。回 True 並更新標記。"""
        today = datetime.date.today()
        with self._lock:
            if self._last_telegram_date == today:
                return False
            self._last_telegram_date = today
            return True


# 模組級 singleton（由 broker factory 初始化時注入 simulation flag）
_guard: Optional[DispositionGuard] = None


def init_guard(simulation: bool) -> DispositionGuard:
    """初始化 singleton；只該在 broker 建立時呼叫一次。"""
    global _guard
    _guard = DispositionGuard(simulation=simulation)
    return _guard


def get_guard() -> Optional[DispositionGuard]:
    """取得 singleton；若尚未 init 回 None（呼叫端要 fallback 為「不擋」以免破壞既有流程）。"""
    return _guard
