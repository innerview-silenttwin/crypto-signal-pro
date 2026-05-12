"""
類股虛擬交易管理器

每個類股擁有獨立的：
- 交易帳戶（餘額、持倉、歷史）
- 策略設定（指標權重、買賣門檻、停損停利）
- 績效追蹤（權益曲線、勝率、損益）

策略隨時可切換，不影響既有帳戶狀態。
"""

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, Optional, List

from notifier import notify_trade, send_telegram

# Broker 抽象層（None 預設 = VirtualBroker，不破壞既有行為）
from brokers.base import Broker, BrokerResult

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sector_accounts")
os.makedirs(DATA_DIR, exist_ok=True)

# ── 預設策略（來自回測最佳結果）──

# 權重依據：chipflow_backtest_20260410 指標歸因分析（2019-2026 七年回測）
# 縮寫：VR=volume_reversal, PS=pullback_support
DEFAULT_STRATEGIES = {
    "半導體": {
        "name": "趨勢追蹤 (EMA+ADX)",
        "param_preset": "標準",
        "weights": {
            'ema_cross': 30.0, 'adx': 28.0, 'macd': 18.0, 'volume_reversal': 12.0,
            'rsi': 10.0, 'volume': 10.0, 'pullback_support': 10.0,
            'stoch_rsi': 6.0, 'bollinger': 2.0, 'mfi': 2.0,
        },
        "buy_threshold": 40,
        "sell_threshold": 40,
        "stop_loss_pct": 8.0,
        "take_profit_pct": 20.0,
        "buy_ratio": 0.10,
        "description": "EMA+ADX 趨勢為核心，MACD 動能輔助（歸因+12.2%勝率），VR/PS 抓拉回反轉，適合半導體成長股",
    },
    "電子代工/零組件": {
        "name": "趨勢追蹤 (EMA+ADX)",
        "param_preset": "寬鬆",
        "weights": {
            'ema_cross': 30.0, 'adx': 27.0, 'pullback_support': 14.0, 'volume_reversal': 12.0,
            'macd': 15.0, 'rsi': 10.0, 'volume': 10.0,
            'stoch_rsi': 6.0, 'bollinger': 2.0, 'mfi': 2.0,
        },
        "buy_threshold": 30,
        "sell_threshold": 30,
        "stop_loss_pct": 10.0,
        "take_profit_pct": 25.0,
        "buy_ratio": 0.08,
        "description": "寬鬆門檻捕捉更多機會，PS 拉回均線支撐（歸因+4.8%勝率）為電子股波動特性加分",
    },
    "金融": {
        "name": "動能+反轉 (MACD+EMA+VR+PS)",
        "param_preset": "標準",
        "weights": {
            'macd': 25.0, 'ema_cross': 25.0, 'rsi': 20.0, 'pullback_support': 17.0,
            'volume_reversal': 12.0, 'stoch_rsi': 10.0, 'volume': 10.0,
            'adx': 10.0, 'bollinger': 2.0, 'mfi': 2.0,
        },
        "buy_threshold": 40,
        "sell_threshold": 40,
        "stop_loss_pct": 8.0,
        "take_profit_pct": 20.0,
        "buy_ratio": 0.10,
        "description": "VR 爆量反轉（歸因+15.4%勝率）+ PS 拉回支撐（+14.9%）為金融股主要信號，MACD/EMA 趨勢過濾",
    },
    "精密機械/工業": {
        "name": "穩健動能 (MACD+EMA+RSI)",
        "param_preset": "標準",
        "weights": {
            'macd': 28.0, 'ema_cross': 28.0, 'rsi': 22.0,
            'pullback_support': 15.0, 'volume_reversal': 8.0,
            'stoch_rsi': 8.0, 'volume': 8.0,
            'adx': 8.0, 'bollinger': 2.0, 'mfi': 2.0,
        },
        "buy_threshold": 40,
        "sell_threshold": 40,
        "stop_loss_pct": 8.0,
        "take_profit_pct": 18.0,
        "buy_ratio": 0.12,
        "description": "MACD+EMA 穩健動能主導（回測亞德客-KY 最佳），RSI 過濾，適合精密製造業高動能走勢",
    },
    "傳產/航運/電信": {
        "name": "趨勢追蹤 (EMA+ADX+Vol)",
        "param_preset": "寬鬆",
        "weights": {
            'ema_cross': 38.0, 'adx': 25.0, 'volume': 19.0, 'macd': 17.0,
            'rsi': 10.0, 'volume_reversal': 10.0, 'stoch_rsi': 6.0,
            'pullback_support': 3.0, 'bollinger': 2.0, 'mfi': 2.0,
        },
        "buy_threshold": 30,
        "sell_threshold": 30,
        "stop_loss_pct": 10.0,
        "take_profit_pct": 25.0,
        "buy_ratio": 0.10,
        "description": "成交量爆發（歸因+26.6%勝率）+ EMA 趨勢主導，PS 大幅降權（回測-10.4%勝率），Regime Layer 停用",
        "layers": {
            "regime": {"enabled": False},
            "fundamental": {"enabled": True},
            "sentiment": {"enabled": True},
            "chipflow": {"enabled": True},
        },
    },
    "其他": {
        "name": "通用均衡策略 (EMA+Vol+VR)",
        "param_preset": "標準",
        "weights": {
            'rsi': 10.0, 'macd': 15.0, 'bollinger': 3.0,
            'mfi': 5.0, 'ema_cross': 18.0, 'volume': 25.0, 'adx': 14.0,
            'stoch_rsi': 8.0, 'volume_reversal': 15.0, 'pullback_support': 14.0,
        },
        "buy_threshold": 35,
        "sell_threshold": 35,
        "stop_loss_pct": 10.0,
        "take_profit_pct": 20.0,
        "buy_ratio": 0.12,
        "description": "通用台股權重，適用生技、ETF 等未分類標的。Volume+VR 量能主導，EMA 過濾趨勢，門檻適中",
    },
}

# ── 類股標的 ──

SECTOR_STOCKS = {
    "半導體": {
        "2330.TW": "台積電", "2454.TW": "聯發科", "2303.TW": "聯電",
        "3711.TW": "日月光投控", "2379.TW": "瑞昱", "3034.TW": "聯詠",
        "6415.TW": "矽力-KY", "2344.TW": "華邦電", "3529.TW": "力旺",
        "5274.TW": "信驊", "2408.TW": "南亞科", "6770.TW": "力積電",
    },
    "電子代工/零組件": {
        "2317.TW": "鴻海", "2382.TW": "廣達", "2308.TW": "台達電",
        "2357.TW": "華碩", "3008.TW": "大立光", "2345.TW": "智邦",
        "3231.TW": "緯創", "2356.TW": "英業達", "4938.TW": "和碩",
        "3443.TW": "創意", "6669.TW": "緯穎",
        "3037.TW": "欣興", "2327.TW": "國巨", "3661.TW": "世芯-KY",
        "2376.TW": "技嘉", "3017.TW": "奇鋐", "2353.TW": "宏碁",
        "6488.TW": "環球晶", "3653.TW": "健策",
        "8046.TW": "南電", "2474.TW": "可成", "2301.TW": "光寶科",
    },
    "精密機械/工業": {
        "1590.TW": "亞德客-KY", "2049.TW": "上銀", "2395.TW": "研華",
        "7769.TW": "鴻勁",
    },
    "金融": {
        "2881.TW": "富邦金", "2882.TW": "國泰金", "2891.TW": "中信金",
        "2886.TW": "兆豐金", "2884.TW": "玉山金", "2880.TW": "華南金",
        "2887.TW": "台新金", "2890.TW": "永豐金", "2883.TW": "開發金",
        "2892.TW": "第一金", "5880.TW": "合庫金", "2885.TW": "元大金",
    },
    "傳產/航運/電信": {
        "1301.TW": "台塑", "2002.TW": "中鋼", "1216.TW": "統一",
        "2603.TW": "長榮", "2609.TW": "陽明", "2615.TW": "萬海",
        "1303.TW": "南亞", "1326.TW": "台化", "1101.TW": "台泥",
        "2207.TW": "和泰車", "9910.TW": "豐泰", "6505.TW": "台塑化",
        "2618.TW": "長榮航",
        "2912.TW": "統一超", "1513.TW": "中興電",
        "2412.TW": "中華電", "3045.TW": "台灣大", "4904.TW": "遠傳",
    },
    "其他": {
        # 生技
        "4743.TW": "合一", "6446.TW": "藥華藥", "1760.TW": "寶齡富錦",
        # ETF
        "0050.TW": "元大台灣50", "0056.TW": "元大高股息",
        "00878.TW": "國泰永續高股息", "00919.TW": "群益台灣精選高息",
    },
}

SECTOR_IDS = {
    "半導體": "semiconductor",
    "電子代工/零組件": "electronics",
    "金融": "finance",
    "傳產/航運/電信": "traditional",
    "精密機械/工業": "precision",
    "其他": "other",
}

SECTOR_ID_TO_NAME = {v: k for k, v in SECTOR_IDS.items()}


def compute_buy_qty_pure(*, price: float, max_order: float, available_cash: float) -> int:
    """純函式：給股價、單筆金額上限、可用現金，回傳買進股數。

    規則：
      - price > 100：零股，買 floor(target_amount / price) 股
      - price ≤ 100：整張，買 floor(target_amount / (price × 1000)) × 1000 股
      - target_amount = min(max_order, available_cash)
      - target_amount ≤ 0 或 price ≤ 0：回 0
    """
    if price <= 0:
        return 0
    target = min(max_order, available_cash)
    if target <= 0:
        return 0
    if price > 100:
        return int(target / price)
    lots = int(target / (price * 1000))
    return lots * 1000


class SectorTradingManager:
    """單一類股的虛擬交易管理器"""

    def __init__(
        self,
        sector_name: str,
        *,
        broker: Optional[Broker] = None,
        risk_gate=None,           # backend.brokers.risk_gate.RiskGate (避免 import 環)
        state_store=None,         # backend.brokers.state_store.BrokerStateStore
    ):
        self.sector_name = sector_name
        self.sector_id = SECTOR_IDS[sector_name]
        self.data_file = os.path.join(DATA_DIR, f"{self.sector_id}_account.json")
        self.stocks = SECTOR_STOCKS[sector_name]
        # 並行控制（daemon thread + Shioaji callback thread 並寫保護）
        self._lock = threading.RLock()
        # broker 預設留 None，由 sector_auto_trader 啟動時注入；測試或舊呼叫路徑可走預設 VirtualBroker
        self._broker: Optional[Broker] = broker
        self._risk_gate = risk_gate
        self._state_store = state_store
        # 去重：同一天 同一 symbol 同一類失敗只發一次 Telegram，避免下單失敗時每 5 分鐘洪水
        # key = (date_str, category, symbol)；跨日自動清理
        self._notif_dedup_today: set = set()

        # 半導體、電子代工初始資金 200 萬，其餘 100 萬
        _init_bal = 2_000_000.0 if sector_name in ("半導體", "電子代工/零組件") else 1_000_000.0

        self.initial_state = {
            "sector_name": sector_name,
            "sector_id": self.sector_id,
            "is_active": True,
            "balance": _init_bal,
            "initial_balance": _init_bal,
            "holdings": {},
            "history": [],
            "equity_curve": [],  # [{"time": "...", "equity": float}]
            "strategy": DEFAULT_STRATEGIES[sector_name].copy(),
            "stocks": list(self.stocks.keys()),
        }
        self.state = self._load()
        if os.path.exists(self.data_file):
            self._last_mtime = os.path.getmtime(self.data_file)
        else:
            self._last_mtime = 0

    def _load(self) -> dict:
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                # 確保 strategy 欄位存在（向後相容）
                if "strategy" not in state:
                    state["strategy"] = DEFAULT_STRATEGIES[self.sector_name].copy()
                if "equity_curve" not in state:
                    state["equity_curve"] = []
                # 同步最新股票清單與類股名稱
                state["stocks"] = list(self.stocks.keys())
                state["sector_name"] = self.sector_name
                state["sector_id"] = self.sector_id
                return state
            except Exception:
                pass
        return self.initial_state.copy()

    def _save(self):
        """Atomic write：避免 daemon thread + Shioaji callback thread 並寫造成損壞。

        失敗時殘留的 .tmp 會被嘗試清掉，但不阻擋例外往上拋。
        """
        with self._lock:
            d = os.path.dirname(self.data_file) or "."
            fd, tmp = tempfile.mkstemp(prefix=f".{self.sector_id}_acct.", suffix=".tmp", dir=d)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, indent=2, ensure_ascii=False)
                os.replace(tmp, self.data_file)
                self._last_mtime = os.path.getmtime(self.data_file)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def _sync_from_disk(self):
        """從磁碟重新載入最新狀態（解決 uvicorn 多 worker 造成的記憶體不一致）"""
        with self._lock:
            if not os.path.exists(self.data_file):
                return
            try:
                mtime = os.path.getmtime(self.data_file)
                if mtime > getattr(self, '_last_mtime', 0):
                    self.state = self._load()
                    self._last_mtime = mtime
            except Exception as e:
                logger.error(f"Sync from disk failed for {self.sector_id}: {e}")

    # ── broker / risk_gate 注入（sector_auto_trader 啟動時呼叫）──

    def attach_broker(self, broker: Broker, risk_gate=None, state_store=None) -> None:
        with self._lock:
            self._broker = broker
            self._risk_gate = risk_gate
            self._state_store = state_store

    def get_broker_name(self) -> str:
        return self._broker.name if self._broker else "virtual_default"

    def get_equity(self) -> float:
        """目前總權益（現金 + 持倉以 avg_price 估值）。給 RiskGate 使用。"""
        self._sync_from_disk()
        with self._lock:
            equity = self.state["balance"]
            for h in self.state["holdings"].values():
                equity += (h.get("qty", 0) or 0) * (h.get("avg_price", 0.0) or 0.0)
            return equity

    def get_position(self, symbol: str) -> dict:
        self._sync_from_disk()
        with self._lock:
            return dict(self.state["holdings"].get(symbol, {}))

    # ── 帳戶控制 ──

    def toggle_active(self, active: bool) -> bool:
        self._sync_from_disk()
        self.state["is_active"] = active
        self._save()
        return active

    def reset_account(self):
        """重置帳戶（保留策略設定）"""
        self._sync_from_disk()
        strategy = self.state.get("strategy", DEFAULT_STRATEGIES[self.sector_name].copy())
        self.state = self.initial_state.copy()
        self.state["strategy"] = strategy
        self._save()

    # ── 策略管理（解耦） ──

    def get_strategy(self) -> dict:
        self._sync_from_disk()
        return self.state.get("strategy", DEFAULT_STRATEGIES[self.sector_name])

    def update_strategy(self, new_strategy: dict):
        """更新策略設定（不影響帳戶狀態）"""
        self._sync_from_disk()
        self.state["strategy"] = new_strategy
        self._save()

    # ── 查詢 ──

    def get_summary(self, current_prices: dict = None) -> dict:
        self._sync_from_disk()
        current_prices = current_prices or {}
        equity = self.state["balance"]
        total_unrealized_pl = 0.0
        unrealized_gain = 0.0
        unrealized_loss = 0.0
        holdings_detail = {}

        for symbol, hold in self.state["holdings"].items():
            if hold["qty"] > 0:
                cur_price = current_prices.get(symbol, hold["avg_price"])
                market_value = hold["qty"] * cur_price
                # 買進總成本（含手續費）：優先用 total_cost，舊資料則估算
                total_cost = hold.get("total_cost", hold["qty"] * hold["avg_price"] + round(hold["qty"] * hold["avg_price"] * 0.001425))
                # 預估賣出淨收入（手續費、證交稅各自四捨五入取整）
                sell_fee = round(market_value * 0.001425)
                sell_tax = round(market_value * 0.003)
                net_sell = market_value - sell_fee - sell_tax
                unrealized_pl = net_sell - total_cost
                equity += market_value
                total_unrealized_pl += unrealized_pl
                if unrealized_pl >= 0:
                    unrealized_gain += unrealized_pl
                else:
                    unrealized_loss += abs(unrealized_pl)
                holdings_detail[symbol] = {
                    **hold,
                    "name": self.stocks.get(symbol, symbol),
                    "current_price": cur_price,
                    "market_value": round(market_value, 2),
                    "total_cost": round(total_cost, 2),
                    "unrealized_pl": round(unrealized_pl, 2),
                }

        initial = self.state.get("initial_balance", 1_000_000.0)

        # 績效統計
        history = self.state.get("history", [])
        closed_trades = [h for h in history if h.get("type") == "SELL"]
        wins = [t for t in closed_trades if t.get("profit", 0) > 0]
        losses = [t for t in closed_trades if t.get("profit", 0) <= 0]
        total_profit = sum(t.get("profit", 0) for t in closed_trades)
        realized_gain = sum(t.get("profit", 0) for t in wins)
        realized_loss = abs(sum(t.get("profit", 0) for t in losses))

        # 累積損益 = 已實現損益 + 未實現損益（含手續費和稅）
        # 這樣才能和交易紀錄加總一致
        total_pl = total_profit + total_unrealized_pl

        return {
            "sector_name": self.sector_name,
            "sector_id": self.sector_id,
            "is_active": self.state["is_active"],
            "balance": round(self.state["balance"], 2),
            "equity": round(equity, 2),
            "initial_balance": initial,
            "total_pl": round(total_pl, 2),
            "total_pl_pct": round(total_pl / initial * 100, 2),
            "holdings": holdings_detail,
            "strategy": self.get_strategy(),
            "stocks": {s: self.stocks.get(s, s) for s in self.state.get("stocks", [])},
            "stats": {
                "total_trades": len(closed_trades),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else 0,
                "realized_profit": round(total_profit, 2),
                "realized_gain": round(realized_gain, 2),
                "realized_loss": round(realized_loss, 2),
                "unrealized_gain": round(unrealized_gain, 2),
                "unrealized_loss": round(unrealized_loss, 2),
            },
            "equity_curve": self.state.get("equity_curve", [])[-100:],  # 最近 100 筆
        }

    def get_history(self, page: int = 1, page_size: int = 50,
                    symbol: str = "", start_date: str = "", end_date: str = "",
                    trade_type: str = "",
                    current_prices: dict = None) -> dict:
        self._sync_from_disk()
        current_prices = current_prices or {}
        history = self.state.get("history", [])

        # ── FIFO 配對：為每筆 BUY 標注已實現/未實現損益 ──
        # 按時間正序走訪（history 存放順序為最新在前）
        chrono = list(reversed(history))
        # 每檔標的的買入批次佇列 {symbol: [(index_in_chrono, remaining_qty, cost_per_unit_incl_fee), ...]}
        buy_lots: dict[str, list] = {}
        # 結果暫存 {id(record): {pnl, pnl_status}}
        pnl_map: dict[int, dict] = {}

        for idx, rec in enumerate(chrono):
            sym = rec.get("symbol", "")
            if rec["type"] == "BUY":
                cost_per_unit = rec.get("cost", rec["price"] * rec["qty"]) / rec["qty"] if rec["qty"] else 0
                buy_lots.setdefault(sym, []).append([idx, rec["qty"], cost_per_unit])
            elif rec["type"] == "SELL":
                lots = buy_lots.get(sym, [])
                sell_qty_remaining = rec["qty"]
                sell_price = rec["price"]
                # 賣出淨價（扣手續費+證交稅）
                gross = sell_qty_remaining * sell_price
                sell_fee = round(gross * 0.001425)
                sell_tax = round(gross * 0.003)
                net_per_unit = (gross - sell_fee - sell_tax) / sell_qty_remaining if sell_qty_remaining else 0

                while sell_qty_remaining > 0 and lots:
                    lot = lots[0]  # [idx, remaining_qty, cost_per_unit]
                    matched_qty = min(lot[1], sell_qty_remaining)
                    realized = round((net_per_unit - lot[2]) * matched_qty)

                    lot_rec = chrono[lot[0]]
                    lot_id = id(lot_rec)
                    if lot_id not in pnl_map:
                        pnl_map[lot_id] = {"pnl": 0, "pnl_status": "realized",
                                           "sold_qty": 0, "total_qty": lot_rec["qty"]}
                    pnl_map[lot_id]["pnl"] += realized
                    pnl_map[lot_id]["sold_qty"] += matched_qty

                    lot[1] -= matched_qty
                    sell_qty_remaining -= matched_qty
                    if lot[1] <= 0:
                        lots.pop(0)

        # 未平倉的買入批次 → 標注未實現損益
        for sym, lots in buy_lots.items():
            for lot in lots:
                if lot[1] <= 0:
                    continue
                lot_rec = chrono[lot[0]]
                lot_id = id(lot_rec)
                cur_price = current_prices.get(sym, lot_rec["price"])
                market_value = lot[1] * cur_price
                sell_fee = round(market_value * 0.001425)
                sell_tax = round(market_value * 0.003)
                unrealized = round((market_value - sell_fee - sell_tax) - lot[1] * lot[2])

                if lot_id in pnl_map:
                    # 部分已賣、部分未賣
                    pnl_map[lot_id]["pnl"] += unrealized
                    pnl_map[lot_id]["pnl_status"] = "partial"
                else:
                    pnl_map[lot_id] = {"pnl": unrealized, "pnl_status": "unrealized",
                                       "sold_qty": 0, "total_qty": lot_rec["qty"]}

        # 將 pnl 資訊寫入副本（不改原始 history）
        annotated = []
        for rec in history:
            rec_copy = dict(rec)
            info = pnl_map.get(id(rec))
            if info and rec["type"] == "BUY":
                rec_copy["pnl"] = info["pnl"]
                rec_copy["pnl_status"] = info["pnl_status"]
                # BUY pnl_pct = pnl / cost × 100
                cost = float(rec.get("cost") or (rec.get("price", 0) * rec.get("qty", 0)))
                if cost > 0:
                    rec_copy["pnl_pct"] = round(info["pnl"] / cost * 100, 2)
            elif rec["type"] == "SELL":
                # SELL profit_pct = profit / 持有成本 × 100
                # 持有成本 = income - profit（從已存欄位反推）
                profit = float(rec.get("profit") or 0)
                income = float(rec.get("income") or 0)
                scaled_cost = income - profit
                if scaled_cost > 0:
                    rec_copy["profit_pct"] = round(profit / scaled_cost * 100, 2)
            annotated.append(rec_copy)

        # ── 篩選 ──
        if symbol:
            annotated = [h for h in annotated if symbol.upper() in h.get("symbol", "").upper()]
        if start_date:
            annotated = [h for h in annotated if h.get("time", "") >= start_date]
        if end_date:
            annotated = [h for h in annotated if h.get("time", "")[:10] <= end_date]
        if trade_type:
            annotated = [h for h in annotated if h.get("type", "").upper() == trade_type.upper()]

        total = len(annotated)
        start = (page - 1) * page_size
        return {"data": annotated[start:start + page_size], "total": total, "page": page, "page_size": page_size}

    # ── 交易執行 ──

    # ── 交易執行（broker 路由 + 風控）──

    def _get_broker(self) -> Broker:
        """Lazy-import VirtualBroker 避免 import cycle 與單元測試時的依賴。"""
        if self._broker is not None:
            return self._broker
        from brokers.virtual import VirtualBroker
        self._broker = VirtualBroker()
        return self._broker

    def _compute_buy_qty(self, price: float, ratio: float) -> int:
        """估算可買股數（純按股價、不依賴帳戶 ratio）。

        規則：
          - price > 100：買零股，數量 = max_order_amount / price 股
          - price ≤ 100：買整數張，floor(max_order_amount / (price × 1000)) 張 × 1000 股
          - 若 RiskGate 沒注入，fallback 用舊規則（ratio × 餘額）保持向下相容
          - 帳戶可用現金（balance × 0.95，留手續費/稅金緩衝）不夠時，自動降到負擔得起

        ratio 參數保留是為了向下相容（VirtualBroker fallback），新邏輯不使用。
        """
        if price <= 0:
            return 0

        # RiskGate 未注入時走舊邏輯（純虛擬模式相容）
        if self._risk_gate is None:
            with self._lock:
                total_equity = self.state["balance"]
                for h in self.state["holdings"].values():
                    total_equity += (h.get("qty", 0) or 0) * (h.get("avg_price", 0.0) or 0.0)
            spend_cash = min(total_equity * ratio, self.state["balance"] * 0.95)
            return int(spend_cash / (price * 1.001425))

        with self._lock:
            available_cash = self.state["balance"] * 0.95
        return compute_buy_qty_pure(
            price=price,
            max_order=float(self._risk_gate.cfg.max_order_amount_twd),
            available_cash=available_cash,
        )

    def _apply_buy_to_ledger(self, symbol: str, qty: int, price: float, signal_desc: str) -> None:
        """成交回填 → 寫帳本。必須持 self._lock。"""
        # 手續費四捨五入取整（台灣實務）
        buy_fee = round(qty * price * 0.001425)
        actual_cost = qty * price + buy_fee
        self.state["balance"] -= actual_cost

        if symbol in self.state["holdings"]:
            old = self.state["holdings"][symbol]
            new_qty = old["qty"] + qty
            new_avg = ((old["qty"] * old["avg_price"]) + (qty * price)) / new_qty
            new_total_cost = old.get(
                "total_cost", old["qty"] * old["avg_price"] * 1.001425
            ) + actual_cost
            self.state["holdings"][symbol] = {
                "qty": new_qty, "avg_price": round(new_avg, 2),
                "total_cost": round(new_total_cost, 2),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        else:
            self.state["holdings"][symbol] = {
                "qty": qty, "avg_price": price,
                "total_cost": round(actual_cost, 2),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        self.state["history"].insert(0, {
            "id": int(time.time() * 1000),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "name": self.stocks.get(symbol, symbol),
            "type": "BUY",
            "price": price,
            "qty": qty,
            "cost": round(actual_cost, 2),
            "signal": signal_desc,
            "balance_after": round(self.state["balance"], 2),
            "broker": self.get_broker_name(),
        })

    def _apply_sell_to_ledger(self, symbol: str, qty: int, price: float, signal_desc: str) -> tuple[float, float]:
        """成交回填 → 寫帳本。回傳 (profit, profit_pct)。必須持 self._lock。"""
        hold = self.state["holdings"].get(symbol, {})
        gross = qty * price
        sell_fee = round(gross * 0.001425)
        sell_tax = round(gross * 0.003)
        net = gross - sell_fee - sell_tax
        # 已實現損益 = 賣出淨收入 - 買進總成本（含買進手續費）
        # 部分賣出時用比例縮放 total_cost
        full_total_cost = hold.get("total_cost", round((hold.get("qty", qty) or qty) * hold.get("avg_price", price) * 1.001425))
        full_qty = hold.get("qty", qty) or qty
        if full_qty <= 0:
            scaled_cost = full_total_cost
        else:
            scaled_cost = full_total_cost * (qty / full_qty)
        profit = net - scaled_cost
        profit_pct = (profit / scaled_cost * 100) if scaled_cost else 0.0

        self.state["balance"] += net
        # 持倉扣除：全賣 → 刪 key；部分賣 → 縮減 qty + total_cost
        new_qty = (hold.get("qty", 0) or 0) - qty
        if new_qty <= 0:
            self.state["holdings"].pop(symbol, None)
        else:
            self.state["holdings"][symbol] = {
                **hold,
                "qty": new_qty,
                "total_cost": round(full_total_cost - scaled_cost, 2),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        self.state["history"].insert(0, {
            "id": int(time.time() * 1000),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "name": self.stocks.get(symbol, symbol),
            "type": "SELL",
            "price": price,
            "qty": qty,
            "income": round(net, 2),
            "profit": round(profit, 2),
            "profit_pct": round(profit_pct, 2),
            "signal": signal_desc,
            "balance_after": round(self.state["balance"], 2),
            "broker": self.get_broker_name(),
        })
        return profit, profit_pct

    def _should_notify_once_today(self, category: str, symbol: str) -> bool:
        """同一天 同一 symbol 同一類別只通知一次。

        broker 失敗 / below_min_lot 在每 5 分鐘輪詢中會持續觸發；若每次都送 Telegram 會被洗版。
        這裡做 in-memory dedup（重啟後會 reset，可接受），跨日自動清掉舊紀錄避免 set 無限長大。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        # lazy GC：清掉非今日的 key
        self._notif_dedup_today = {k for k in self._notif_dedup_today if k[0] == today}
        key = (today, category, symbol)
        if key in self._notif_dedup_today:
            return False
        self._notif_dedup_today.add(key)
        return True

    def _handle_skipped(self, symbol: str, action: str, reason: str, *,
                         qty: int = 0, price: float = 0.0,
                         needed: float = 0.0, available: float = 0.0,
                         signal_desc: str = "") -> None:
        """RiskGate / broker 拒單時的統一處理：JSONL log + Telegram 通知。

        通知策略：對影響可交易性的原因主動推播，並附上股票、價量、原因、觸發信號。
          - below_min_lot / below_min_order_amount → 銀彈不足通知
          - dust_position                          → 通知（持倉尾數無法賣，需要手動處理）
          - daily_locked                           → kill-switch 通知（每日一次）
          - broker_*                               → 券商異常通知
          - pending_exists* / cooldown_* / over_*  → 不通知（routine reject）
          - no_position / market_closed / holiday  → 不通知
        """
        rec = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sector_id": self.sector_id,
            "symbol": symbol,
            "action": action,
            "reason": reason,
            "qty_shares": int(qty),
            "limit_price": round(price, 2),
            "order_amount_twd": round(qty * price, 0),
            "needed_twd": round(needed, 0),
            "available_twd": round(available, 0),
            "signal": signal_desc,
        }
        try:
            log_path = os.path.join(DATA_DIR, "..", "skipped_trades.jsonl")
            log_path = os.path.normpath(log_path)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"skipped_trades log failed: {e}")

        stock_name = self.stocks.get(symbol, symbol)
        code = symbol.replace(".TW", "").replace(".TWO", "")
        stock_url = f"https://tw.stock.yahoo.com/quote/{code}.TW"

        # 共用的「動作+股數+價位+小計」資訊行
        if qty > 0 and price > 0:
            order_line = f"動作：{action} {qty:,} 股 @ ${price:,.2f}（小計 ${int(qty * price):,}）"
        elif qty > 0:
            order_line = f"動作：{action} {qty:,} 股"
        else:
            order_line = f"動作：{action}"

        if reason == "below_min_lot":
            if self._should_notify_once_today("below_min_lot", symbol):
                send_telegram(
                    f"⚠️ <b>銀彈不足</b> [{self.sector_name}]\n"
                    f"標的：<a href=\"{stock_url}\">{stock_name}({code})</a>\n"
                    f"{order_line}\n"
                    f"原因：股數 < 10（10 股需 ~${int(needed):,}，可用 ~${int(available):,}）\n"
                    f"觸發信號：{signal_desc}"
                )
        elif reason.startswith("below_min_order_amount"):
            if self._should_notify_once_today("below_min_order_amount", symbol):
                send_telegram(
                    f"⚠️ <b>銀彈不足</b> [{self.sector_name}]\n"
                    f"標的：<a href=\"{stock_url}\">{stock_name}({code})</a>\n"
                    f"{order_line}\n"
                    f"原因：金額低於單筆下限 ~${int(needed):,}（可用 ~${int(available):,}）\n"
                    f"觸發信號：{signal_desc}"
                )
        elif reason.startswith("dust_position"):
            if self._should_notify_once_today("dust_position", symbol):
                send_telegram(
                    f"⚠️ <b>持倉尾數無法賣出</b> [{self.sector_name}]\n"
                    f"標的：<a href=\"{stock_url}\">{stock_name}({code})</a>\n"
                    f"{order_line}\n"
                    f"原因：持倉 {qty} 股 < 10 股下限（永豐不收）\n"
                    f"請手動處理此 dust 部位\n"
                    f"觸發信號：{signal_desc}"
                )
        elif reason.startswith("daily_locked"):
            if self._should_notify_once_today("daily_locked", "*"):
                send_telegram(
                    f"🛑 <b>每日 kill-switch 啟動</b>\n"
                    f"類股：{self.sector_name}\n"
                    f"理由：{reason}\n"
                    f"剩餘交易暫停至明日"
                )
        elif reason.startswith("broker_"):
            if self._should_notify_once_today("broker_error", symbol):
                send_telegram(
                    f"🚨 <b>券商下單異常</b> [{self.sector_name}]\n"
                    f"標的：<a href=\"{stock_url}\">{stock_name}({code})</a>\n"
                    f"{order_line}\n"
                    f"原因：{reason}\n"
                    f"觸發信號：{signal_desc}\n"
                    f"⚠️ 連續發生請檢查 Shioaji API 相容性 / 網路 / 帳號狀態"
                )

    def execute_trade(
        self,
        symbol: str,
        trade_type: str,
        price: float,
        signal_desc: str,
        ratio: float = 0.20,
        *,
        is_auto_stop: bool = False,
    ) -> bool:
        """執行交易。

        Args:
            ratio: BUY 用，單檔投入比例（5 檔平分 = 0.20）
            is_auto_stop: True 表示停損/停利自動觸發；風險閘對「除權息凍結」會擋此類 SELL

        流程：估算 qty → RiskGate.allow → 寫 pending → broker.submit → 寫 ledger / 清 pending → cooldown / 通知
        """
        self._sync_from_disk()
        broker = self._get_broker()

        # ── 1. 估算 qty ──
        if trade_type == "BUY":
            if self.state["balance"] < 100:
                return False
            qty = self._compute_buy_qty(price, ratio)
            if qty <= 0:
                return False
        elif trade_type == "SELL":
            hold = self.state["holdings"].get(symbol)
            if not hold or (hold.get("qty", 0) or 0) <= 0:
                return False
            qty = hold["qty"]
        else:
            return False

        # ── 2. RiskGate（若已注入）──
        if self._risk_gate is not None:
            decision = self._risk_gate.allow(
                sector_id=self.sector_id,
                symbol=symbol,
                action=trade_type,
                qty_shares=qty,
                limit_price=price,
                is_auto_stop=is_auto_stop,
            )
            if not decision.ok:
                self._handle_skipped(
                    symbol, trade_type, decision.reason,
                    qty=qty, price=price,
                    needed=decision.needed_twd, available=decision.available_twd,
                    signal_desc=signal_desc,
                )
                return False

        # ── 3. pending order（state_store 若已注入）──
        # 用 try_reserve_for_symbol 做 atomic check-and-insert，修補與 RiskGate.allow 之間的 TOCTOU race
        # （兩個 thread 同時對同一 symbol 跑 execute_trade 時，只有一個能搶到 reservation）
        client_order_id = uuid.uuid4().hex
        if self._state_store is not None:
            from brokers.state_store import PendingOrder
            reserved = self._state_store.try_reserve_for_symbol(PendingOrder(
                client_order_id=client_order_id,
                sector_id=self.sector_id,
                symbol=symbol,
                action=trade_type,
                qty_shares=qty,
                limit_price=price,
                submitted_at=time.time(),
                notes=signal_desc[:120],
            ))
            if not reserved:
                # 另一個 thread 已經為同一 symbol 開了 in-flight 訂單；這次跳過
                self._handle_skipped(
                    symbol, trade_type, "pending_exists_race",
                    qty=qty, price=price,
                    signal_desc=signal_desc,
                )
                return False

        # ── 4. 送出 ──
        try:
            result: BrokerResult = broker.submit(
                symbol=symbol,
                action=trade_type,
                qty_shares=qty,
                limit_price=price,
                client_order_id=client_order_id,
                sector_id=self.sector_id,
                signal_desc=signal_desc,
            )
        except Exception as e:
            logger.exception("broker.submit raised: %s", e.__class__.__name__)
            if self._state_store is not None:
                self._state_store.remove_pending(client_order_id)
            # 透過 _handle_skipped 走統一格式（含 dedup、price/qty）
            self._handle_skipped(
                symbol, trade_type, f"broker_exception:{e.__class__.__name__}",
                qty=qty, price=price,
                signal_desc=signal_desc,
            )
            return False
        finally:
            # 確保 pending 在「成交/拒絕後同步點」之前一定會清掉（reconcile 也會兜底）
            pass

        # ── 5. 處理結果 ──
        if not result.ok:
            if self._state_store is not None:
                self._state_store.remove_pending(client_order_id)
            self._handle_skipped(
                symbol, trade_type, f"broker_{result.fill_status}:{result.reason}",
                qty=qty, price=price,
                signal_desc=signal_desc,
            )
            return False

        actual_qty = result.actual_qty if result.actual_qty > 0 else qty
        actual_price = result.actual_price if result.actual_price > 0 else price

        with self._lock:
            if trade_type == "BUY":
                self._apply_buy_to_ledger(symbol, actual_qty, actual_price, signal_desc)
                profit_twd: Optional[float] = None
                profit_pct: Optional[float] = None
            else:  # SELL
                profit_twd, profit_pct = self._apply_sell_to_ledger(
                    symbol, actual_qty, actual_price, signal_desc
                )
            self._save()

        if self._state_store is not None:
            self._state_store.remove_pending(client_order_id)

        if self._risk_gate is not None:
            self._risk_gate.record_success(self.sector_id, symbol, trade_type)
            if trade_type == "SELL" and profit_twd is not None:
                self._risk_gate.maybe_trigger_kill_switch(self.sector_id, profit_twd)

        # 通知 + console
        if trade_type == "BUY":
            print(f"[{self.sector_name}] BUY {actual_qty} {symbol} @ {actual_price} ({broker.name})")
            notify_trade(
                self.sector_name, symbol, self.stocks.get(symbol, symbol),
                "BUY", actual_price, actual_qty, signal_desc,
            )
        else:
            print(
                f"[{self.sector_name}] SELL {actual_qty} {symbol} @ {actual_price} "
                f"(P&L: {profit_twd:+.0f} / {profit_pct:+.2f}%) ({broker.name})"
            )
            notify_trade(
                self.sector_name, symbol, self.stocks.get(symbol, symbol),
                "SELL", actual_price, actual_qty, signal_desc,
                profit=profit_twd, profit_pct=profit_pct,
            )
        return True

    def record_equity(self, current_prices: dict = None):
        """記錄當前權益到曲線（只在有即時價格時記錄，避免假波動）"""
        self._sync_from_disk()
        current_prices = current_prices or {}
        holdings = self.state["holdings"]

        # 如果有持倉但沒有任何即時價格，跳過記錄（避免 fallback 到 avg_price 造成假波動）
        if holdings and not any(s in current_prices for s in holdings):
            return

        equity = self.state["balance"]
        for symbol, hold in holdings.items():
            price = current_prices.get(symbol, hold["avg_price"])
            equity += hold["qty"] * price

        # 避免與上一筆重複（同分鐘內不重複記錄）
        curve = self.state.setdefault("equity_curve", [])
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        if curve and curve[-1]["time"] == now_str:
            curve[-1]["equity"] = round(equity, 2)
        else:
            curve.append({
                "time": now_str,
                "equity": round(equity, 2),
            })
        # 最多保留 500 筆
        if len(self.state["equity_curve"]) > 500:
            self.state["equity_curve"] = self.state["equity_curve"][-500:]
        self._save()


# ── 啟動時把 settings.json 的自選股注入回 SECTOR_STOCKS ──
# 否則重啟後，從後台 /api/settings/stock 新增的股票會從交易中心消失

def _inject_custom_stocks_into_sectors():
    try:
        settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "settings.json")
        if not os.path.exists(settings_path):
            return
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        for s in settings.get("custom_stocks", []):
            sec = s.get("sector")
            sym = s.get("symbol")
            name = s.get("name") or sym
            if sec in SECTOR_STOCKS and sym:
                SECTOR_STOCKS[sec][sym] = name
    except Exception as e:
        print(f"[sector_trader] inject custom_stocks failed: {e}")


_inject_custom_stocks_into_sectors()


# ── 全域 6 個交易管理器實例 ──

sector_managers: Dict[str, SectorTradingManager] = {}
for _sector_name in SECTOR_IDS:
    sector_managers[SECTOR_IDS[_sector_name]] = SectorTradingManager(_sector_name)


def get_manager(sector_id: str) -> Optional[SectorTradingManager]:
    return sector_managers.get(sector_id)


def get_all_managers() -> Dict[str, SectorTradingManager]:
    return sector_managers
