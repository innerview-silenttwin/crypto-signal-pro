import json
import os
import time
from datetime import datetime

TRADING_DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trading_account.json")

class TradingManager:
    def __init__(self):
        self.initial_config = {
            "is_active": False,
            "balance": 1000000.0,
            "holdings": {},  # { "2330.TW": { "qty": 0, "avg_price": 0 } }
            "history": [],
            "symbols": ["2330.TW", "2317.TW", "BTC/USDT"]  # 預設監控標的
        }
        self.state = self.load_state()

    def load_state(self):
        if os.path.exists(TRADING_DATA_FILE):
            try:
                with open(TRADING_DATA_FILE, "r") as f:
                    return json.load(f)
            except:
                return self.initial_config
        return self.initial_config

    def save_state(self):
        with open(TRADING_DATA_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def toggle_active(self, status: bool):
        self.state["is_active"] = status
        self.save_state()
        return self.state["is_active"]

    def get_summary(self, current_prices: dict):
        equity = self.state["balance"]
        for symbol, hold in self.state["holdings"].items():
            if hold["qty"] > 0:
                price = current_prices.get(symbol, hold["avg_price"])
                equity += hold["qty"] * price
        
        return {
            "is_active": self.state["is_active"],
            "balance": round(self.state["balance"], 2),
            "equity": round(equity, 2),
            "unrealized_pl": round(equity - 1000000.0, 2),
            "holdings": self.state["holdings"]
        }

    def execute_trade(self, symbol: str, trade_type: str, price: float, signal_desc: str, ratio: float = 0.95):
        """
        執行交易
        :param ratio: 投入資金比例 (0.0 ~ 1.0)
        """
        # 計算當前總資產 (用來決定投入金額)
        # 這裡為了簡單，我們粗估總資產 = 現金 + 現有持倉成本
        total_equity = self.state["balance"]
        for s in self.state["holdings"]:
            h = self.state["holdings"][s]
            total_equity += h["qty"] * h["avg_price"]

        if trade_type == "BUY":
            if self.state["balance"] < 100: return False
            
            # 投入金額 = 總資產 * 比例 (最高不超過現有現金)
            target_spend = total_equity * ratio
            spend_cash = min(target_spend, self.state["balance"] * 0.99) # 留 1% 緩衝
            
            fee = spend_cash * 0.001425
            qty = int((spend_cash - fee) / price)
            
            if qty <= 0: return False
            
            actual_cost = (qty * price) + fee
            self.state["balance"] -= actual_cost
            
            # 更新持倉 (如果已有持倉則平均成本，但策略上通常是新的)
            if symbol in self.state["holdings"]:
                old = self.state["holdings"][symbol]
                new_qty = old["qty"] + qty
                new_avg = ((old["qty"] * old["avg_price"]) + (qty * price)) / new_qty
                self.state["holdings"][symbol] = {
                    "qty": new_qty,
                    "avg_price": round(new_avg, 2),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            else:
                self.state["holdings"][symbol] = {
                    "qty": qty,
                    "avg_price": price,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }

            log_entry = {
                "id": int(time.time()),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol,
                "type": "BUY",
                "price": price,
                "qty": qty,
                "cost": round(actual_cost, 2),
                "signal": signal_desc,
                "balance_after": round(self.state["balance"], 2)
            }
            self.state["history"].insert(0, log_entry)
            self.save_state()
            return True

        elif trade_type == "SELL":
            hold = self.state["holdings"].get(symbol)
            if not hold or hold["qty"] <= 0: return False
            
            qty = hold["qty"]
            # 賣出手續費 + 稅
            gross_income = qty * price
            fee_tax = gross_income * (0.001425 + 0.003)
            net_income = gross_income - fee_tax
            
            self.state["balance"] += net_income
            del self.state["holdings"][symbol]
            
            log_entry = {
                "id": int(time.time()),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol,
                "type": "SELL",
                "price": price,
                "qty": qty,
                "income": round(net_income, 2),
                "profit": round(net_income - (qty * hold["avg_price"]), 2),
                "signal": signal_desc,
                "balance_after": round(self.state["balance"], 2)
            }
            self.state["history"].insert(0, log_entry)
            self.save_state()
            print(f"[TRADE] SOLD {qty} {symbol} at {price}")
            return True
        
        return False

    def add_symbol(self, symbol: str):
        if symbol not in self.state["symbols"]:
            self.state["symbols"].append(symbol)
            self.save_state()
            return True
        return False

    def remove_symbol(self, symbol: str):
        if symbol in self.state["symbols"]:
            self.state["symbols"].remove(symbol)
            self.save_state()
            return True
        return False

# 全域實例
trading_manager = TradingManager()
