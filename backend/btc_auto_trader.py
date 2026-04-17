"""
BTC 自動交易引擎（虛擬交易）

四策略並行監控，任一觸發即交易，通知標明觸發策略與回測績效：

策略 #1 日線35+CryptoFlow — +131.8% 勝率52.9% 34筆 Sharpe0.22
策略 #2 日線40 純技術    — +120.0% 勝率75.0%  8筆 Sharpe0.71
策略 #3 日線40+CryptoFlow — +119.7% 勝率54.5% 22筆 Sharpe0.25
策略 #4 日線35 純技術    — +116.1% 勝率46.7% 30筆 Sharpe0.24

停損 12% / 停利 25%

運作方式：
1. 背景執行緒每小時檢查一次
2. 從 Binance 取得最新 250 根日線 K 棒
3. 四策略各自計算信號（不同門檻 × 有無 CryptoFlowLayer）
4. 任一策略達門檻即觸發交易，通知中列出所有觸發策略及其回測結果
"""

import sys
import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from layers.crypto_flow import CryptoFlowLayer
from notifier import send_telegram

logger = logging.getLogger(__name__)

# ── 常量 ──

SYMBOL = "BTC/USDT"
TIMEFRAME = "1d"
STOP_LOSS_PCT = 12.0
TAKE_PROFIT_PCT = 25.0
TRADE_FEE_PCT = 0.1  # Binance 0.1% 手續費

# ── 四策略定義（含回測績效）──

STRATEGIES = [
    {
        "id": "S1",
        "name": "日線35+CryptoFlow",
        "buy_threshold": 35.0,
        "sell_threshold": 35.0,
        "use_flow": True,
        "backtest": "+131.8% | 勝率52.9% | 34筆 | Sharpe 0.22",
    },
    {
        "id": "S2",
        "name": "日線40 純技術",
        "buy_threshold": 40.0,
        "sell_threshold": 40.0,
        "use_flow": False,
        "backtest": "+120.0% | 勝率75.0% | 8筆 | Sharpe 0.71",
    },
    {
        "id": "S3",
        "name": "日線40+CryptoFlow",
        "buy_threshold": 40.0,
        "sell_threshold": 40.0,
        "use_flow": True,
        "backtest": "+119.7% | 勝率54.5% | 22筆 | Sharpe 0.25",
    },
    {
        "id": "S4",
        "name": "日線35 純技術",
        "buy_threshold": 35.0,
        "sell_threshold": 35.0,
        "use_flow": False,
        "backtest": "+116.1% | 勝率46.7% | 30筆 | Sharpe 0.24",
    },
]

ACCOUNT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "btc_trading_account.json"
)

DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data"
)


# ═══════════════════════════════════════════════
# BTC 帳戶管理
# ═══════════════════════════════════════════════

class BTCAccount:
    """BTC 虛擬交易帳戶"""

    DEFAULT = {
        "is_active": False,
        "initial_balance": 100000.0,  # 10 萬 USDT
        "balance": 100000.0,
        "holdings": {},   # {"BTC/USDT": {"qty": 0.5, "avg_price": 85000, "time": "..."}}
        "history": [],
        "equity_curve": [],
    }

    def __init__(self):
        self.state = self._load()

    def _load(self) -> dict:
        if os.path.exists(ACCOUNT_FILE):
            try:
                with open(ACCOUNT_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return dict(self.DEFAULT)

    def _save(self):
        os.makedirs(os.path.dirname(ACCOUNT_FILE), exist_ok=True)
        with open(ACCOUNT_FILE, "w") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    @property
    def is_active(self) -> bool:
        return self.state.get("is_active", False)

    def toggle(self, active: bool):
        self.state["is_active"] = active
        self._save()

    def get_holding_for_strat(self, strat_id: str) -> Optional[dict]:
        return self.state["holdings"].get(f"{SYMBOL}_{strat_id}")

    def buy_strat(self, price: float, signal_desc: str, strat_id: str, spend_amount: float) -> Optional[dict]:
        """按策略獨立買入 BTC"""
        if self.state["balance"] < spend_amount:
            spend_amount = self.state["balance"] * 0.95
            
        if spend_amount < 100:
            return None

        fee = spend_amount * TRADE_FEE_PCT / 100
        qty = (spend_amount - fee) / price

        if qty <= 0:
            return None

        actual_cost = qty * price + fee
        self.state["balance"] -= actual_cost

        # 更新策略獨立持倉
        hold_key = f"{SYMBOL}_{strat_id}"
        hold = self.state["holdings"].get(hold_key)
        if hold and hold["qty"] > 0:
            new_qty = hold["qty"] + qty
            new_avg = (hold["qty"] * hold["avg_price"] + qty * price) / new_qty
            self.state["holdings"][hold_key] = {
                "qty": round(new_qty, 8),
                "avg_price": round(new_avg, 2),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "strat_id": strat_id,
            }
        else:
            self.state["holdings"][hold_key] = {
                "qty": round(qty, 8),
                "avg_price": round(price, 2),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "strat_id": strat_id,
            }

        entry = {
            "id": int(time.time()),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": f"{SYMBOL} ({strat_id})",
            "type": "BUY",
            "price": round(price, 2),
            "qty": round(qty, 8),
            "cost": round(actual_cost, 2),
            "fee": round(fee, 2),
            "signal": signal_desc,
            "balance_after": round(self.state["balance"], 2),
            "strat_id": strat_id
        }
        self.state["history"].insert(0, entry)
        self._save()
        return entry

    def sell_strat(self, price: float, signal_desc: str, strat_id: str) -> Optional[dict]:
        """賣出指定策略的全部持倉"""
        hold_key = f"{SYMBOL}_{strat_id}"
        hold = self.state["holdings"].get(hold_key)
        if not hold or hold["qty"] <= 0:
            return None

        qty = hold["qty"]
        gross = qty * price
        fee = gross * TRADE_FEE_PCT / 100
        net = gross - fee
        profit = net - (qty * hold["avg_price"])

        self.state["balance"] += net
        del self.state["holdings"][hold_key]

        entry = {
            "id": int(time.time()),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": f"{SYMBOL} ({strat_id})",
            "type": "SELL",
            "price": round(price, 2),
            "qty": round(qty, 8),
            "income": round(net, 2),
            "fee": round(fee, 2),
            "profit": round(profit, 2),
            "signal": signal_desc,
            "balance_after": round(self.state["balance"], 2),
            "strat_id": strat_id
        }
        self.state["history"].insert(0, entry)
        self._save()
        return entry

    def get_summary(self, current_price: float = None) -> dict:
        equity = self.state["balance"]
        unrealized = 0.0
        
        for key, hold in self.state["holdings"].items():
            if hold["qty"] > 0 and current_price:
                market_value = hold["qty"] * current_price
                equity += market_value
                unrealized += market_value - hold["qty"] * hold["avg_price"]

        return {
            "is_active": self.is_active,
            "balance": round(self.state["balance"], 2),
            "equity": round(equity, 2),
            "initial_balance": self.state.get("initial_balance", 100000),
            "total_return_pct": round((equity / self.state.get("initial_balance", 100000) - 1) * 100, 2),
            "unrealized_pl": round(unrealized, 2),
            "holdings": self.state["holdings"],
            "trade_count": len(self.state["history"]),
        }

    def record_equity(self, price: float):
        """記錄權益曲線"""
        summary = self.get_summary(price)
        self.state["equity_curve"].append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "equity": summary["equity"],
            "price": round(price, 2),
        })
        # 保留最近 365 筆
        self.state["equity_curve"] = self.state["equity_curve"][-365:]
        self._save()


# ═══════════════════════════════════════════════
# 資料取得
# ═══════════════════════════════════════════════

def fetch_btc_daily(limit: int = 250) -> Optional[pd.DataFrame]:
    """從 Binance 公開 API 取得 BTC/USDT 日線（無需 API Key）"""
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": "BTCUSDT", "interval": "1d", "limit": limit}
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        rows = []
        for k in data:
            rows.append({
                "timestamp": pd.to_datetime(k[0], unit="ms"),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })

        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        return df
    except Exception as e:
        logger.error(f"取得 BTC 日線失敗: {e}")
        return None


def fetch_btc_price() -> Optional[float]:
    """取得 BTC 最新價格"""
    try:
        url = "https://api.binance.com/api/v3/ticker/price"
        resp = requests.get(url, params={"symbol": "BTCUSDT"}, timeout=10)
        return float(resp.json()["price"])
    except Exception:
        return None


def update_flow_data():
    """更新恐懼貪婪指數（每日更新即可）"""
    fng_path = os.path.join(DATA_DIR, "btc_fear_greed.csv")
    try:
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=7&format=json", timeout=10
        )
        new_data = resp.json()["data"]
        rows = []
        for d in new_data:
            rows.append({
                "timestamp": pd.to_datetime(int(d["timestamp"]), unit="s"),
                "fng_value": int(d["value"]),
                "fng_class": d["value_classification"],
            })
        df_new = pd.DataFrame(rows).set_index("timestamp")

        if os.path.exists(fng_path):
            df_old = pd.read_csv(fng_path, index_col="timestamp", parse_dates=True)
            df = pd.concat([df_old, df_new])
            df = df[~df.index.duplicated(keep="last")]
            df.sort_index(inplace=True)
        else:
            df = df_new.sort_index()
        df.to_csv(fng_path)
    except Exception as e:
        logger.warning(f"更新恐懼貪婪指數失敗: {e}")

    # 更新資金費率
    fr_path = os.path.join(DATA_DIR, "btc_funding_rate.csv")
    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 10},
            timeout=10,
        )
        rows = []
        for d in resp.json():
            rows.append({
                "timestamp": pd.to_datetime(d["fundingTime"], unit="ms"),
                "funding_rate": float(d["fundingRate"]),
            })
        df_new = pd.DataFrame(rows).set_index("timestamp")

        if os.path.exists(fr_path):
            df_old = pd.read_csv(fr_path, index_col="timestamp", parse_dates=True)
            df = pd.concat([df_old, df_new])
            df = df[~df.index.duplicated(keep="last")]
            df.sort_index(inplace=True)
        else:
            df = df_new.sort_index()
        df.to_csv(fr_path)
    except Exception as e:
        logger.warning(f"更新資金費率失敗: {e}")


# ═══════════════════════════════════════════════
# 交易邏輯
# ═══════════════════════════════════════════════

latest_strategy_scores = {
    "flow": {"buy": 0.0, "sell": 0.0},
    "pure": {"buy": 0.0, "sell": 0.0}
}

def check_and_trade(account: BTCAccount):
    """核心：四策略並行檢查信號並執行獨立買賣交易"""
    global latest_strategy_scores

    # 1. 取得最新日線
    df = fetch_btc_daily(250)
    if df is None or len(df) < 200:
        logger.warning("無法取得足夠 K 線資料")
        return

    current_price = df["close"].iloc[-1]

    # 2. 更新 Flow 資料
    update_flow_data()

    # 3. 計算兩種信號（有/無 CryptoFlow）
    aggregator = SignalAggregator()
    crypto_flow = CryptoFlowLayer(data_dir=DATA_DIR)

    signal_with_flow = aggregator.analyze(df.copy(), SYMBOL, TIMEFRAME, layers=[crypto_flow])
    signal_pure_tech = SignalAggregator().analyze(df.copy(), SYMBOL, TIMEFRAME, layers=None)
    
    # 紀錄最新分數供前端顯示
    latest_strategy_scores["flow"]["buy"] = round(signal_with_flow.buy_score, 1)
    latest_strategy_scores["flow"]["sell"] = round(signal_with_flow.sell_score, 1)
    latest_strategy_scores["pure"]["buy"] = round(signal_pure_tech.buy_score, 1)
    latest_strategy_scores["pure"]["sell"] = round(signal_pure_tech.sell_score, 1)

    # 4. 獨立策略檢查
    # 各策略配置本金的 25% 作為單一操作的動用金上限
    initial_alloc = account.state.get("initial_balance", 100000) * 0.25

    for strat in STRATEGIES:
        holding = account.get_holding_for_strat(strat["id"])
        sig = signal_with_flow if strat["use_flow"] else signal_pure_tech

        if holding is None or holding["qty"] <= 0:
            # === 無持倉 → 檢查買入 ===
            if sig.direction == "BUY" and sig.confidence >= strat["buy_threshold"]:
                desc = (f"買入信號 (買{sig.buy_score:.0f}/賣{sig.sell_score:.0f}, "
                        f"{sig.signal_level}) [{strat['id']}]")
                entry = account.buy_strat(current_price, desc, strat["id"], initial_alloc)
                if entry:
                    entry["buy_score"] = round(sig.buy_score, 1)
                    entry["sell_score"] = round(sig.sell_score, 1)
                    entry["signal_level"] = sig.signal_level
                    entry["triggered_strategies"] = strat["id"]
                    account._save()
                    
                    mock_triggered = [{
                        "id": strat["id"], "name": strat["name"],
                        "score": sig.confidence, "backtest": strat["backtest"],
                        "use_flow": strat["use_flow"]
                    }]
                    logger.info(f"🟢 買入 BTC ({strat['id']}) @ ${current_price:,.0f}")
                    _notify_btc_trade("BUY", entry, mock_triggered, signal_with_flow)
        else:
            # === 有持倉 → 檢查賣出 ===
            avg_price = holding["avg_price"]
            change_pct = (current_price - avg_price) / avg_price * 100

            should_sell = False
            sell_reason = ""

            if change_pct <= -STOP_LOSS_PCT:
                should_sell = True
                sell_reason = f"觸發停損 ({change_pct:+.1f}%)"
            elif change_pct >= TAKE_PROFIT_PCT:
                should_sell = True
                sell_reason = f"觸發停利 ({change_pct:+.1f}%)"
            elif sig.direction == "SELL" and sig.confidence >= strat["sell_threshold"]:
                should_sell = True
                sell_reason = (f"賣出信號 (買{sig.buy_score:.0f}/賣{sig.sell_score:.0f}, "
                               f"{sig.signal_level}) [{strat['id']}]")

            if should_sell:
                entry = account.sell_strat(current_price, sell_reason, strat["id"])
                if entry:
                    entry["buy_score"] = round(sig.buy_score, 1)
                    entry["sell_score"] = round(sig.sell_score, 1)
                    entry["signal_level"] = sig.signal_level
                    entry["triggered_strategies"] = strat["id"]
                    entry["change_pct"] = round(change_pct, 2)
                    account._save()
                    
                    mock_triggered = [{
                        "id": strat["id"], "name": strat["name"],
                        "score": sig.confidence, "backtest": strat["backtest"],
                        "use_flow": strat["use_flow"]
                    }]
                    logger.info(f"🔴 賣出 BTC ({strat['id']}) @ ${current_price:,.0f} ({sell_reason})")
                    _notify_btc_trade("SELL", entry, mock_triggered, signal_with_flow)

    # 5. 記錄權益
    account.record_equity(current_price)


def _notify_btc_trade(trade_type: str, entry: dict, triggered: list, signal):
    """Telegram 通知（含觸發策略與回測績效）"""
    emoji = "\U0001f7e2" if trade_type == "BUY" else "\U0001f534"
    action = "買入" if trade_type == "BUY" else "賣出"

    lines = [
        f"{emoji} <b>BTC {action}通知</b>",
        f"價格：${entry['price']:,.2f}",
        f"數量：{entry['qty']:.6f} BTC",
    ]

    if trade_type == "BUY":
        lines.append(f"成本：${entry['cost']:,.2f}")
    else:
        lines.append(f"收入：${entry['income']:,.2f}")
        if "profit" in entry:
            pnl_emoji = "\U0001f4c8" if entry["profit"] >= 0 else "\U0001f4c9"
            lines.append(f"損益：{pnl_emoji} ${entry['profit']:,.2f}")

    lines.append(f"原因：{entry['signal']}")
    lines.append(f"餘額：${entry['balance_after']:,.2f}")

    # 觸發策略明細
    if triggered:
        lines.append("")
        lines.append(f"<b>觸發策略 ({len(triggered)}/{len(STRATEGIES)})</b>")
        for t in triggered:
            lines.append(f"  [{t['id']}] {t['name']} {t['score']:.0f}分")
            lines.append(f"      回測: {t['backtest']}")

    # Flow 資訊
    for mod in signal.layer_modifiers:
        if mod.layer_name == "crypto_flow" and mod.active:
            fng = mod.details.get("fear_greed", "N/A")
            fr = mod.details.get("funding_rate", "N/A")
            lines.append(f"\n恐懼貪婪: {fng} | 費率: {fr}%")

    send_telegram("\n".join(lines))


# ═══════════════════════════════════════════════
# 背景守護程式
# ═══════════════════════════════════════════════

class BTCAutoTrader:
    """BTC 自動交易背景守護程式"""

    def __init__(self, interval_seconds: int = 3600):
        """
        Args:
            interval_seconds: 檢查間隔（預設 1 小時）
        """
        self.interval = interval_seconds
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.account = BTCAccount()
        self.last_run_time: Optional[str] = None
        self.last_signal: Optional[str] = None

    def start(self):
        if self._running:
            return False
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"🚀 BTC 自動交易已啟動 (間隔: {self.interval}秒)")
        return True

    def stop(self):
        self._running = False
        logger.info("⏹️ BTC 自動交易已停止")
        return True

    @property
    def is_running(self) -> bool:
        return self._running

    def _loop(self):
        # 啟動後等 10 秒再開始（讓 server 先準備好）
        time.sleep(10)
        while self._running:
            if self.account.is_active:
                try:
                    check_and_trade(self.account)
                    self.last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                except Exception as e:
                    logger.error(f"BTC 交易錯誤: {e}")
                    self.last_signal = f"error: {e}"
            time.sleep(self.interval)

    def run_once(self):
        """手動觸發一次檢查"""
        if self.account.is_active:
            check_and_trade(self.account)
            self.last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def get_status(self) -> dict:
        price = fetch_btc_price()
        summary = self.account.get_summary(price)

        strat_list = []
        for s in STRATEGIES:
            t = "flow" if s["use_flow"] else "pure"
            strat_list.append({
                "id": s["id"], 
                "name": s["name"],
                "buy_threshold": s["buy_threshold"],
                "sell_threshold": s["sell_threshold"],
                "use_flow": s["use_flow"], 
                "backtest": s["backtest"],
                "current_buy_score": latest_strategy_scores[t]["buy"],
                "current_sell_score": latest_strategy_scores[t]["sell"]
            })

        return {
            "is_running": self._running,
            "is_active": self.account.is_active,
            "interval_seconds": self.interval,
            "last_run_time": self.last_run_time,
            "btc_price": price,
            "strategies": strat_list,
            "stop_loss_pct": STOP_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            **summary,
        }


# 全域實例
btc_trader = BTCAutoTrader(interval_seconds=3600)
