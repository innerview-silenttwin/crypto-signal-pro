"""
類股虛擬交易管理器

每個類股擁有獨立的：
- 交易帳戶（餘額、持倉、歷史）
- 策略設定（指標權重、買賣門檻、停損停利）
- 績效追蹤（權益曲線、勝率、損益）

策略隨時可切換，不影響既有帳戶狀態。
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, Optional, List

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sector_accounts")
os.makedirs(DATA_DIR, exist_ok=True)

# ── 預設策略（來自回測最佳結果）──

DEFAULT_STRATEGIES = {
    "半導體": {
        "name": "趨勢追蹤 (EMA+ADX)",
        "param_preset": "標準",
        "weights": {
            'rsi': 10.0, 'macd': 15.0, 'bollinger': 5.0,
            'mfi': 5.0, 'ema_cross': 30.0, 'volume': 10.0, 'adx': 25.0,
        },
        "buy_threshold": 40,
        "sell_threshold": 40,
        "stop_loss_pct": 8.0,
        "take_profit_pct": 20.0,
        "description": "以 EMA 交叉 + ADX 趨勢強度為核心，適合半導體成長股趨勢明確的特性",
    },
    "電子代工/零組件": {
        "name": "趨勢追蹤 (EMA+ADX)",
        "param_preset": "寬鬆",
        "weights": {
            'rsi': 10.0, 'macd': 15.0, 'bollinger': 5.0,
            'mfi': 5.0, 'ema_cross': 30.0, 'volume': 10.0, 'adx': 25.0,
        },
        "buy_threshold": 30,
        "sell_threshold": 30,
        "stop_loss_pct": 10.0,
        "take_profit_pct": 25.0,
        "description": "寬鬆門檻捕捉更多交易機會，適合電子股波動較大的特性",
    },
    "金融": {
        "name": "動能+趨勢 (RSI+MACD+EMA)",
        "param_preset": "標準",
        "weights": {
            'rsi': 20.0, 'macd': 25.0, 'bollinger': 5.0,
            'mfi': 5.0, 'ema_cross': 25.0, 'volume': 10.0, 'adx': 10.0,
        },
        "buy_threshold": 40,
        "sell_threshold": 40,
        "stop_loss_pct": 8.0,
        "take_profit_pct": 20.0,
        "description": "RSI+MACD 動能搭配 EMA 趨勢，高勝率適合金融股穩健波動",
    },
    "傳產/航運": {
        "name": "趨勢追蹤 (EMA+ADX)",
        "param_preset": "寬鬆",
        "weights": {
            'rsi': 10.0, 'macd': 15.0, 'bollinger': 5.0,
            'mfi': 5.0, 'ema_cross': 30.0, 'volume': 10.0, 'adx': 25.0,
        },
        "buy_threshold": 30,
        "sell_threshold": 30,
        "stop_loss_pct": 10.0,
        "take_profit_pct": 25.0,
        "description": "寬鬆趨勢追蹤，抓住航運等週期股的大波段行情。Regime Layer 停用（回測顯示景氣循環股加盤勢層反而有害）",
        "layers": {
            "regime": {"enabled": False},
            "fundamental": {"enabled": True},
            "sentiment": {"enabled": True},
            "chipflow": {"enabled": True},
        },
    },
}

# ── 類股標的 ──

SECTOR_STOCKS = {
    "半導體": {
        "2330.TW": "台積電", "2454.TW": "聯發科", "2303.TW": "聯電",
        "3711.TW": "日月光投控", "2379.TW": "瑞昱",
    },
    "電子代工/零組件": {
        "2317.TW": "鴻海", "2382.TW": "廣達", "2308.TW": "台達電",
        "2357.TW": "華碩", "3008.TW": "大立光",
    },
    "金融": {
        "2881.TW": "富邦金", "2882.TW": "國泰金", "2891.TW": "中信金",
        "2886.TW": "兆豐金", "2884.TW": "玉山金",
    },
    "傳產/航運": {
        "1301.TW": "台塑", "2002.TW": "中鋼", "1216.TW": "統一",
        "2603.TW": "長榮", "2412.TW": "中華電",
    },
}

SECTOR_IDS = {
    "半導體": "semiconductor",
    "電子代工/零組件": "electronics",
    "金融": "finance",
    "傳產/航運": "traditional",
}

SECTOR_ID_TO_NAME = {v: k for k, v in SECTOR_IDS.items()}


class SectorTradingManager:
    """單一類股的虛擬交易管理器"""

    def __init__(self, sector_name: str):
        self.sector_name = sector_name
        self.sector_id = SECTOR_IDS[sector_name]
        self.data_file = os.path.join(DATA_DIR, f"{self.sector_id}_account.json")
        self.stocks = SECTOR_STOCKS[sector_name]

        self.initial_state = {
            "sector_name": sector_name,
            "sector_id": self.sector_id,
            "is_active": False,
            "balance": 1_000_000.0,
            "initial_balance": 1_000_000.0,
            "holdings": {},
            "history": [],
            "equity_curve": [],  # [{"time": "...", "equity": float}]
            "strategy": DEFAULT_STRATEGIES[sector_name].copy(),
            "stocks": list(self.stocks.keys()),
        }
        self.state = self._load()

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
                return state
            except Exception:
                pass
        return self.initial_state.copy()

    def _save(self):
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    # ── 帳戶控制 ──

    def toggle_active(self, active: bool) -> bool:
        self.state["is_active"] = active
        self._save()
        return active

    def reset_account(self):
        """重置帳戶（保留策略設定）"""
        strategy = self.state.get("strategy", DEFAULT_STRATEGIES[self.sector_name].copy())
        self.state = self.initial_state.copy()
        self.state["strategy"] = strategy
        self._save()

    # ── 策略管理（解耦） ──

    def get_strategy(self) -> dict:
        return self.state.get("strategy", DEFAULT_STRATEGIES[self.sector_name])

    def update_strategy(self, new_strategy: dict):
        """更新策略設定（不影響帳戶狀態）"""
        self.state["strategy"] = new_strategy
        self._save()

    # ── 查詢 ──

    def get_summary(self, current_prices: dict = None) -> dict:
        current_prices = current_prices or {}
        equity = self.state["balance"]
        total_unrealized_pl = 0.0
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
            },
            "equity_curve": self.state.get("equity_curve", [])[-100:],  # 最近 100 筆
        }

    def get_history(self, page: int = 1, page_size: int = 15,
                    symbol: str = "", start_date: str = "", end_date: str = "") -> dict:
        history = self.state.get("history", [])
        if symbol:
            history = [h for h in history if symbol.upper() in h.get("symbol", "").upper()]
        if start_date:
            history = [h for h in history if h.get("time", "") >= start_date]
        if end_date:
            history = [h for h in history if h.get("time", "")[:10] <= end_date]

        total = len(history)
        start = (page - 1) * page_size
        return {"data": history[start:start + page_size], "total": total, "page": page}

    # ── 交易執行 ──

    def execute_trade(self, symbol: str, trade_type: str, price: float,
                      signal_desc: str, ratio: float = 0.20) -> bool:
        """
        執行交易（每檔標的配置 ~20% 資金）

        ratio: 單檔投入比例（5檔平分 = 0.20）
        """
        if trade_type == "BUY":
            if self.state["balance"] < 100:
                return False

            # 單檔投入金額
            total_equity = self.state["balance"]
            for h in self.state["holdings"].values():
                total_equity += h["qty"] * h["avg_price"]

            spend_cash = min(total_equity * ratio, self.state["balance"] * 0.95)
            # 先估算可買股數（預留手續費空間）
            qty = int(spend_cash / (price * 1.001425))
            if qty <= 0:
                return False

            # 手續費四捨五入取整（台灣實務）
            buy_fee = round(qty * price * 0.001425)
            actual_cost = qty * price + buy_fee
            self.state["balance"] -= actual_cost

            if symbol in self.state["holdings"]:
                old = self.state["holdings"][symbol]
                new_qty = old["qty"] + qty
                new_avg = ((old["qty"] * old["avg_price"]) + (qty * price)) / new_qty
                new_total_cost = old.get("total_cost", old["qty"] * old["avg_price"] * 1.001425) + actual_cost
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
            })
            self._save()
            print(f"[{self.sector_name}] BUY {qty} {symbol} @ {price}")
            return True

        elif trade_type == "SELL":
            hold = self.state["holdings"].get(symbol)
            if not hold or hold["qty"] <= 0:
                return False

            qty = hold["qty"]
            gross = qty * price
            # 手續費、證交稅各自四捨五入取整（台灣實務）
            sell_fee = round(gross * 0.001425)
            sell_tax = round(gross * 0.003)
            net = gross - sell_fee - sell_tax
            # 已實現損益 = 賣出淨收入 - 買進總成本（含買進手續費）
            total_cost = hold.get("total_cost", round(qty * hold["avg_price"] * 1.001425))
            profit = net - total_cost

            self.state["balance"] += net
            del self.state["holdings"][symbol]

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
                "signal": signal_desc,
                "balance_after": round(self.state["balance"], 2),
            })
            self._save()
            print(f"[{self.sector_name}] SELL {qty} {symbol} @ {price} (P&L: {profit:+.0f})")
            return True

        return False

    def record_equity(self, current_prices: dict = None):
        """記錄當前權益到曲線（只在有即時價格時記錄，避免假波動）"""
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


# ── 全域 4 個交易管理器實例 ──

sector_managers: Dict[str, SectorTradingManager] = {}
for _sector_name in SECTOR_IDS:
    sector_managers[SECTOR_IDS[_sector_name]] = SectorTradingManager(_sector_name)


def get_manager(sector_id: str) -> Optional[SectorTradingManager]:
    return sector_managers.get(sector_id)


def get_all_managers() -> Dict[str, SectorTradingManager]:
    return sector_managers
