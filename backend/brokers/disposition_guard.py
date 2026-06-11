"""處置股 (disposition stock) 偵測與預收券 (reserve_stock) 流程。

背景（2026-06-09/11 永豐客服澄清）：
- 台股「處置股」期間（通常 10 個交易日）採「分盤管制撮合」+「圈存門檻」
- 5 分鐘處置（一般）：單筆 ≥ unit_limit 或累計 ≥ total_limit 才需 reserve_stock
- 20 分鐘處置（加重）：unit_limit / total_limit = None，每筆都要 reserve
- ⚠️ sim 環境 reserve_stock 是 no-op（永遠回 status=False），所以 sim SELL 必失敗
- 2026-06-11 客服 dump 確認 punish() 物件含 9 個 parallel list 屬性

整合點：backend/brokers/sinopac.py::submit() 在 place_order 前呼叫
ensure_sellable()，回 False 就 skip 不送單。

未來：持倉變大（單檔 > 10 張）時要評估「累計門檻」追蹤。目前持倉小故不做。

詳見 memory/project_sinopac_punish_handling.md + project_sinopac_qa_log.md。
"""

from __future__ import annotations

import datetime
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)


# 預收券 API 服務時段（永豐客服 2026-06-11 確認）
RESERVE_START = datetime.time(8, 0)
RESERVE_END = datetime.time(14, 30)

# punish() 物件要抽出的屬性（客服 2026-06-11 確認的完整 schema）
_INFO_FIELDS = (
    "start_date",
    "end_date",
    "announced_date",
    "interval",
    "unit_limit",
    "total_limit",
    "description",
)

# 1 張 = 1000 股（台股慣例）
SHARES_PER_LOT = 1000


class DispositionGuard:
    """處置股清單快取 + 賣出前預收券檢查。

    Thread-safe（cache 操作整段持鎖，含 punish() 網路呼叫，避免 race）。
    每日首次 query 自動重新拉清單（用日期當 cache key）。
    若 punish() 失敗，沿用舊清單並標 ok=False（prod 上線時用此旗標保守擋單）。
    """

    def __init__(self, simulation: bool):
        self._simulation = bool(simulation)
        # _lock 保護 cache（_date/_info/_ok）讀寫；不可持鎖跨網路呼叫。
        self._lock = threading.Lock()
        # _refresh_lock 序列化 punish() 呼叫，避免多執行緒 cold-start 重複打 API。
        self._refresh_lock = threading.Lock()
        self._date: Optional[datetime.date] = None
        # code → {start_date, end_date, announced_date, interval, unit_limit, total_limit, description}
        self._info: dict[str, dict[str, Any]] = {}
        self._ok = False  # punish() 是否曾成功載入
        self._last_telegram_date: Optional[datetime.date] = None

    @staticmethod
    def _normalize(symbol: str) -> str:
        """去掉 .TW / .TWO 後綴；punish() 回的 code 是純數字。"""
        return symbol.split(".")[0]

    @staticmethod
    def _build_info_dict(p) -> dict[str, dict[str, Any]]:
        """從 punish() 回的物件抽出 per-code info dict。

        客服 dump 確認：屬性都是平行 list，同 index 對應同一檔。
        """
        codes = list(getattr(p, "code", []) or [])
        if not codes:
            return {}
        out = {}
        for i, code in enumerate(codes):
            entry = {}
            for field in _INFO_FIELDS:
                values = getattr(p, field, None) or []
                entry[field] = values[i] if i < len(values) else None
            out[code] = entry
        return out

    def _ensure_today(self, api) -> dict[str, dict[str, Any]]:
        """lazy refresh：今日尚未載入才呼叫 punish()。

        ⚠️ 不可持 self._lock 跨 api.punish() 呼叫（broker 若 hang 會卡住所有
        其它讀路徑，包含 UI 端點）。設計：
        1. 短暫 self._lock 看 cache 是否已是今日 → 是就直接回
        2. _refresh_lock 序列化「真正打 API」這段，避免多執行緒 cold-start 重複 punish
        3. _refresh_lock 內再 double-check cache（其它 thread 可能剛剛 fill 完）
        4. 真打 api.punish() 時**完全不持鎖**
        5. 拿到 result 後再短暫 self._lock 寫進 cache
        """
        today = datetime.date.today()

        # Fast path: cache 已是今日
        with self._lock:
            if self._date == today:
                return dict(self._info)

        # Slow path: 序列化 refresh
        with self._refresh_lock:
            # 別的 thread 剛剛幫忙 refresh 完了嗎？
            with self._lock:
                if self._date == today:
                    return dict(self._info)

            # 真打 API（不持鎖）
            try:
                p = api.punish()
                info = self._build_info_dict(p)
                ok = True
                err = None
            except Exception as e:
                info = None
                ok = False
                err = e

            # 寫進 cache（短暫持鎖）
            with self._lock:
                if ok:
                    self._date = today
                    self._info = info
                    self._ok = True
                    logger.info("處置股清單更新 %s，共 %d 檔: %s",
                                today, len(info), sorted(info.keys()))
                    return dict(info)
                self._ok = False
                logger.error("punish() 失敗，沿用舊清單(%s): %r", self._date, err)
                return dict(self._info)

    def get_disposition_set(self, api) -> set[str]:
        """回傳今日處置股 code set（純數字、無 .TW）。"""
        info = self._ensure_today(api)
        return set(info.keys())

    def get_disposition_info(self, api, symbol: str) -> Optional[dict[str, Any]]:
        """取得單一處置股的完整資訊；非處置股回 None。"""
        code = self._normalize(symbol)
        info = self._ensure_today(api)
        entry = info.get(code)
        return dict(entry) if entry else None

    def is_disposed(self, api, symbol: str) -> bool:
        """symbol 是否為處置股（會 lazy refresh 清單）。"""
        code = self._normalize(symbol)
        info = self._ensure_today(api)
        return code in info

    @staticmethod
    def _in_reserve_hours(now: Optional[datetime.datetime] = None) -> bool:
        """是否在預收服務時段 08:00 ~ 14:30。"""
        t = (now or datetime.datetime.now()).time()
        return RESERVE_START <= t <= RESERVE_END

    @staticmethod
    def _need_reserve(entry: dict[str, Any], qty_lots: float) -> bool:
        """依處置等級 + 單筆張數判斷是否需呼叫 reserve_stock。

        規則（客服 2026-06-11 確認）：
        - 加重處置（unit_limit / total_limit = None）：每筆都要 reserve
        - 一般處置：單筆 ≥ unit_limit 才要 reserve
          - 累計門檻 total_limit **目前不追蹤**

        ⚠️ **TODO(disposition-total-limit)**: 持倉變大時的已知技術債。
        現況：我們系統單檔持倉多在 1-4 張，遠低於 total_limit（通常 30 張）。
        風險點：若未來單檔倉位 > 10 張，且當日多次 SELL，可能累計超過
                total_limit 但本函式仍回 False，prod broker 端會拒。
        修法：加 _daily_sent_lots: Dict[date, Dict[code, float]] 追蹤累計，
              比對 (sent + qty_lots) >= total_limit 也回 True。
              對應 memory/project_sinopac_punish_handling.md 待辦項。

        Args:
            entry: punish info dict (含 unit_limit, total_limit)
            qty_lots: 訂單張數（不是股數！1 張 = 1000 股）
        """
        unit_limit = entry.get("unit_limit")
        # 加重處置：unit_limit 為 None，每筆都要 reserve
        if unit_limit is None:
            return True
        # 一般處置：單筆 ≥ unit_limit 才要 reserve
        return qty_lots >= unit_limit

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

        # lazy refresh（_ensure_today 自管 lock，不可外面再持鎖）
        info = self._ensure_today(api)
        entry = info.get(code)
        with self._lock:
            disp_ok = self._ok

        # 非處置股 → 直接放行
        if entry is None:
            return True, ""

        # sim：reserve_stock 是 no-op，分盤撮合也沒模擬，直接 skip
        if self._simulation:
            return False, f"disposition_sim_noop:{code}"

        # prod：清單若從沒成功載入 → 保守擋掉（客服建議）
        if not disp_ok:
            return False, f"disposition_list_not_loaded:{code}"

        # 服務時段檢查
        if not self._in_reserve_hours(now):
            return False, f"disposition_out_of_hours:{code}"

        # 判斷單筆需不需要走 reserve_stock
        qty_lots = qty_shares / SHARES_PER_LOT
        if not self._need_reserve(entry, qty_lots):
            # 小單未達門檻 → 直接送 SELL，不用 reserve
            logger.info(
                "[PROD] %s 處置股 (%s) 但單筆 %.2f 張 < unit_limit %s，直接 SELL",
                code, entry.get("interval"), qty_lots, entry.get("unit_limit"),
            )
            return True, ""

        # 達門檻：真送 reserve_stock
        try:
            contract = api.Contracts.Stocks[code]
            resp = api.reserve_stock(contract, qty_shares, account=api.stock_account)
            status = getattr(getattr(resp, "response", None), "status", False)
            if status:
                logger.info("[PROD] %s 預收券成功 share=%d (interval=%s)",
                            code, qty_shares, entry.get("interval"))
                return True, ""
            info_msg = getattr(getattr(resp, "response", None), "info", "")
            logger.error("[PROD] %s 預收券失敗 info=%r", code, info_msg)
            return False, f"reserve_stock_status_false:{code}"
        except Exception as e:
            logger.exception("[PROD] %s 預收券例外: %r", code, e)
            return False, f"reserve_stock_exception:{code}:{e.__class__.__name__}"

    # ── 對外查詢（給 UI / telegram 用）──

    def snapshot(self) -> dict:
        """回 cache 當前狀態（不觸發 refresh）。給 UI / telegram 顯示用。

        Returns:
            {
                "date": "2026-06-11" or None,
                "ok": bool,
                "count": int,
                "stocks": {
                    "6770": {
                        "start_date": "2026-06-02",  # ISO format
                        "end_date": "2026-06-15",
                        "interval": "5分鐘",
                        "unit_limit": 10.0,
                        "total_limit": 30.0,
                        ...
                    },
                    ...
                }
            }
        """
        with self._lock:
            stocks = {}
            for code, entry in self._info.items():
                stocks[code] = {
                    "start_date": _iso(entry.get("start_date")),
                    "end_date": _iso(entry.get("end_date")),
                    "announced_date": _iso(entry.get("announced_date")),
                    "interval": entry.get("interval"),
                    "unit_limit": entry.get("unit_limit"),
                    "total_limit": entry.get("total_limit"),
                    "description": entry.get("description"),
                }
            return {
                "date": self._date.isoformat() if self._date else None,
                "ok": self._ok,
                "count": len(self._info),
                "stocks": stocks,
            }

    def should_send_daily_telegram(self) -> bool:
        """每日只該發一次處置股摘要 telegram。回 True 並更新標記。"""
        today = datetime.date.today()
        with self._lock:
            if self._last_telegram_date == today:
                return False
            self._last_telegram_date = today
            return True


def _iso(d: Any) -> Optional[str]:
    """date / datetime → ISO 字串；其它型別原樣回傳。"""
    if d is None:
        return None
    if isinstance(d, (datetime.date, datetime.datetime)):
        return d.isoformat()
    return str(d)


# 模組級 singleton（由 broker factory 初始化時注入 simulation flag）
_guard: Optional[DispositionGuard] = None


def init_guard(simulation: bool) -> DispositionGuard:
    """初始化 singleton；只該在 broker 建立時呼叫一次。"""
    global _guard
    _guard = DispositionGuard(simulation=simulation)
    return _guard


def get_guard() -> Optional[DispositionGuard]:
    """取得 singleton；尚未 init 回 None（呼叫端 fallback 為「不擋」以免破壞既有流程）。"""
    return _guard
