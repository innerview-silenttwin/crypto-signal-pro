"""
類股自動交易背景守護程式

功能：
1. 定時輪詢每個類股的標的
2. 用各自的策略權重計算信號
3. 達到門檻自動執行買賣
4. 檢查停損/停利條件
5. 記錄權益曲線

設計原則：策略與帳戶解耦，策略可隨時更換不影響既有持倉。
"""

import sys
import os
import time
import threading
from datetime import datetime
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from sector_trader import (
    get_all_managers, SectorTradingManager,
    SECTOR_STOCKS, SECTOR_IDS,
)
from layers import RegimeLayer, LayerRegistry


# ── 行情快取 ──

_price_cache: Dict[str, Dict] = {}  # symbol -> {"price": float, "time": float, "df": DataFrame}
CACHE_TTL = 120  # 秒


def fetch_latest_price(symbol: str) -> Optional[float]:
    """取得最新收盤價（含快取）"""
    now = time.time()
    if symbol in _price_cache and now - _price_cache[symbol]["time"] < CACHE_TTL:
        return _price_cache[symbol]["price"]

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d", interval="1d")
        if hist.empty:
            return None
        hist.columns = [c.lower() for c in hist.columns]
        price = float(hist['close'].iloc[-1])
        _price_cache[symbol] = {"price": price, "time": now}
        return price
    except Exception as e:
        print(f"  ⚠️ 取價失敗 {symbol}: {e}")
        return None


def fetch_signal_data(symbol: str, lookback_days: int = 250) -> Optional[pd.DataFrame]:
    """取得用於信號計算的歷史數據"""
    now = time.time()
    cache_key = symbol
    if cache_key in _price_cache and "df" in _price_cache[cache_key]:
        cached = _price_cache[cache_key]
        if now - cached["time"] < CACHE_TTL:
            return cached["df"]

    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=f"{lookback_days}d", interval="1d")
        if df.empty or len(df) < 50:
            return None
        df.columns = [c.lower() for c in df.columns]
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        df = df[df['volume'] > 0]

        if cache_key not in _price_cache:
            _price_cache[cache_key] = {}
        _price_cache[cache_key]["df"] = df
        _price_cache[cache_key]["price"] = float(df['close'].iloc[-1])
        _price_cache[cache_key]["time"] = now
        return df
    except Exception as e:
        print(f"  ⚠️ 取數據失敗 {symbol}: {e}")
        return None


# ── 信號計算 ──

def compute_signal(df: pd.DataFrame, weights: dict, symbol: str,
                    layers=None, sector_id: str = "") -> Optional[dict]:
    """計算信號分數（含分析層修正）"""
    try:
        aggregator = SignalAggregator(weights=weights)
        signal = aggregator.analyze(
            df.copy(), symbol, "1d",
            layers=layers, sector_id=sector_id,
        )
        return {
            "direction": signal.direction,
            "confidence": signal.confidence,
            "buy_score": signal.buy_score,
            "sell_score": signal.sell_score,
            "raw_buy_score": signal.raw_buy_score,
            "raw_sell_score": signal.raw_sell_score,
            "signal_level": signal.signal_level,
            "regime": signal.regime,
            "layer_reasons": [m.reason for m in signal.layer_modifiers if m.reason],
            "summary": signal.summary(),
        }
    except Exception as e:
        print(f"  ⚠️ 信號計算錯誤 {symbol}: {e}")
        return None


# ── 單一類股交易循環 ──

def build_layers(strategy: dict) -> list:
    """根據策略配置建立分析層"""
    layers_config = strategy.get("layers", {"regime": {"enabled": True}})
    layers = []

    # Regime layer（預設啟用）
    regime_cfg = layers_config.get("regime", {"enabled": True})
    if regime_cfg.get("enabled", True):
        layers.append(RegimeLayer(enabled=True))

    return layers


def process_sector(manager: SectorTradingManager):
    """處理單一類股的交易邏輯（含盤勢辨識層）"""
    if not manager.state["is_active"]:
        return

    strategy = manager.get_strategy()
    weights = strategy["weights"]
    buy_th = strategy["buy_threshold"]
    sell_th = strategy["sell_threshold"]
    stop_loss = strategy["stop_loss_pct"]
    take_profit = strategy["take_profit_pct"]

    # 建立分析層
    layers = build_layers(strategy)

    current_prices = {}

    for symbol in manager.state.get("stocks", []):
        # 1. 取得數據
        df = fetch_signal_data(symbol)
        if df is None:
            continue

        price = float(df['close'].iloc[-1])
        current_prices[symbol] = price

        # 2. 計算信號（含分析層修正）
        sig = compute_signal(df, weights, symbol,
                             layers=layers, sector_id=manager.sector_id)
        if sig is None:
            continue

        # Log regime info
        if sig.get("regime"):
            regime_reasons = sig.get("layer_reasons", [])
            reason_str = " | ".join(regime_reasons) if regime_reasons else ""
            print(f"  [{manager.sector_name}] {symbol} 盤勢:{sig['regime']} "
                  f"買:{sig['buy_score']:.0f}(原{sig['raw_buy_score']:.0f}) "
                  f"賣:{sig['sell_score']:.0f}(原{sig['raw_sell_score']:.0f}) "
                  f"{reason_str}")

        # 3. 檢查停損/停利（已持倉）
        hold = manager.state["holdings"].get(symbol)
        if hold and hold["qty"] > 0:
            pnl_pct = (price - hold["avg_price"]) / hold["avg_price"] * 100

            if pnl_pct <= -stop_loss:
                manager.execute_trade(
                    symbol, "SELL", price,
                    f"停損觸發 ({pnl_pct:.1f}%)"
                )
                continue
            elif pnl_pct >= take_profit:
                manager.execute_trade(
                    symbol, "SELL", price,
                    f"停利觸發 ({pnl_pct:.1f}%)"
                )
                continue

        # 4. 信號交易（含盤勢修正後的分數）
        regime_tag = f" [{sig['regime']}]" if sig.get("regime") else ""
        if hold and hold["qty"] > 0:
            # 已持倉 → 只看賣出信號
            if sig["direction"] == "SELL" and sig["confidence"] >= sell_th:
                desc = f"賣出信號 ({sig['confidence']:.0f}分, {sig['signal_level']}){regime_tag}"
                manager.execute_trade(symbol, "SELL", price, desc)
        else:
            # 無持倉 → 只看買入信號
            if sig["direction"] == "BUY" and sig["confidence"] >= buy_th:
                desc = f"買入信號 ({sig['confidence']:.0f}分, {sig['signal_level']}){regime_tag}"
                ratio = 0.20
                manager.execute_trade(symbol, "BUY", price, desc, ratio=ratio)

    # 5. 記錄權益
    manager.record_equity(current_prices)


# ── 背景守護程式 ──

class SectorAutoTrader:
    """背景自動交易守護程式"""

    def __init__(self, interval_seconds: int = 300):
        """
        Args:
            interval_seconds: 輪詢間隔（預設 5 分鐘，實際交易建議 15~60 分鐘）
        """
        self.interval = interval_seconds
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.last_run_time: Optional[str] = None
        self.last_run_status: Dict[str, str] = {}

    def start(self):
        if self._running:
            return False
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"🚀 類股自動交易已啟動 (間隔: {self.interval}秒)")
        return True

    def stop(self):
        self._running = False
        print("⏹️  類股自動交易已停止")
        return True

    @property
    def is_running(self) -> bool:
        return self._running

    def _loop(self):
        while self._running:
            try:
                self._run_once()
            except Exception as e:
                print(f"❌ 自動交易錯誤: {e}")
            time.sleep(self.interval)

    def _run_once(self):
        """執行一輪所有類股檢查"""
        self.last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        managers = get_all_managers()
        active_count = 0

        for sector_id, manager in managers.items():
            if manager.state["is_active"]:
                active_count += 1
                try:
                    process_sector(manager)
                    self.last_run_status[sector_id] = "ok"
                except Exception as e:
                    self.last_run_status[sector_id] = f"error: {e}"
                    print(f"  ❌ {manager.sector_name} 交易錯誤: {e}")
            else:
                self.last_run_status[sector_id] = "inactive"

        if active_count > 0:
            print(f"  ✅ 完成一輪檢查 ({active_count} 個類股, {self.last_run_time})")

    def run_once_now(self):
        """手動觸發一次（非背景）"""
        self._run_once()

    def get_status(self) -> dict:
        return {
            "is_running": self._running,
            "interval_seconds": self.interval,
            "last_run_time": self.last_run_time,
            "last_run_status": self.last_run_status,
        }


# 全域實例
auto_trader = SectorAutoTrader(interval_seconds=300)
