"""
類股自動交易背景守護程式

功能：
1. 定時輪詢每個類股的標的
2. 用各自的策略權重計算信號
3. 達到門檻自動執行買賣
4. 檢查停損/停利條件
5. 記錄權益曲線

設計原則：策略與帳戶解耦，策略可隨時更換不影響既有持倉。

交易決策雙軌制（信號分數 + 綜合分數）：
- 信號分數（技術面 buy/sell score，經各分析層乘數/偏移修正）→ 決定進出場時機
- 綜合分數（五維加權平均：籌碼+技術+基本面+盤勢+消息）→ 決定標的品質
- 買入條件：信號分數 ≥ 門檻 AND 綜合分數 ≥ 50
- 賣出/停損/停利：僅用信號分數（不受綜合分數限制）
"""

import sys
import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd
import pytz
import yfinance as yf

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from sector_trader import (
    get_all_managers, SectorTradingManager,
    SECTOR_STOCKS, SECTOR_IDS,
)
from layers import RegimeLayer, FundamentalLayer, SentimentLayer, ChipFlowLayer, LayerRegistry
from screener import get_symbol_sector, SECTOR_COMPOSITE_WEIGHTS


# ── 行情快取 ──

_price_cache: Dict[str, Dict] = {}  # symbol -> {"price": float, "time": float, "df": DataFrame}
CACHE_TTL = 120  # 秒

# ── 本地資料路徑（與走勢圖系統共用） ──

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(_BASE_DIR, "data", "history", "stock")
LAST_PRICES_FILE = os.path.join(_BASE_DIR, "data", "last_prices.json")

# ── 持久化最後已知價格 ──

_last_prices: Dict[str, dict] = {}  # symbol -> {"price": float, "date": "YYYY-MM-DD", "time": unix}


def _load_last_prices():
    """啟動時從磁碟載入上次 auto_trader 確認的收盤價"""
    global _last_prices
    if os.path.exists(LAST_PRICES_FILE):
        try:
            with open(LAST_PRICES_FILE, "r") as f:
                _last_prices = json.load(f)
        except Exception:
            _last_prices = {}


def _save_last_prices(prices: Dict[str, float], dates: Dict[str, str] = None):
    """auto_trader 確認的收盤價 → 寫入磁碟（重啟後可用）

    dates: symbol → 價格的實際交易日期 (YYYY-MM-DD)，避免用舊日期資料覆蓋新的
    """
    global _last_prices
    dates = dates or {}
    now = time.time()
    for sym, price in prices.items():
        date_str = dates.get(sym, "")
        # 若已有更新日期的價格 → 不覆蓋
        existing = _last_prices.get(sym)
        if existing and date_str and existing.get("date", "") > date_str:
            continue
        _last_prices[sym] = {"price": price, "date": date_str, "time": now}
    try:
        with open(LAST_PRICES_FILE, "w") as f:
            json.dump(_last_prices, f, indent=2)
    except Exception as e:
        logger.warning(f"save_last_prices failed: {e}")


# ── 本地 CSV 讀寫（與走勢圖 L2 cache 共用） ──

def _safe_filename(symbol: str) -> str:
    return symbol.replace("/", "_").replace(".", "_")


def _load_local_csv(symbol: str) -> Optional[pd.DataFrame]:
    """讀取本地 CSV 歷史資料"""
    path = os.path.join(HISTORY_DIR, f"{_safe_filename(symbol)}.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col not in df.columns:
                return None
        df = df[df['volume'] > 0]
        return df
    except Exception:
        return None


def _save_local_csv(symbol: str, df: pd.DataFrame):
    """更新本地 CSV（走勢圖系統也會受惠）"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"{_safe_filename(symbol)}.csv")
    try:
        out = df[['open', 'high', 'low', 'close', 'volume']].copy()
        out.index.name = 'date'
        out.to_csv(path)
        logger.debug(f"[local-csv] Saved {len(out)} rows → {path}")
    except Exception as e:
        logger.warning(f"save_local_csv failed {symbol}: {e}")


# 模組載入時讀取持久化價格
_load_last_prices()


def fetch_latest_price(symbol: str) -> Optional[float]:
    """取得最新收盤價（含快取）。
    只回傳有效的最新收盤：若 yfinance 最後一筆日期早於今天（台股時間），
    代表今日資料尚未更新，回傳 None 避免使用過時價格。
    """
    now = time.time()
    if symbol in _price_cache and now - _price_cache[symbol]["time"] < CACHE_TTL:
        return _price_cache[symbol]["price"]

    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d", interval="1d")
        if hist.empty:
            return None
        hist.columns = [c.lower() for c in hist.columns]

        # 去除 NaN 後取最後一筆
        valid = hist['close'].dropna()
        if valid.empty:
            return None

        last_date = valid.index[-1]
        price = float(valid.iloc[-1])

        # 若最新收盤日期 < 今日台灣日期，代表 yfinance 資料尚未更新
        # 回傳 None，讓呼叫端改用買入均價，避免跨日比較產生假損益
        tw_tz = pytz.timezone("Asia/Taipei")
        today_tw = datetime.now(tw_tz).date()
        last_date_tw = last_date.astimezone(tw_tz).date() if hasattr(last_date, 'astimezone') else last_date.date()

        if last_date_tw < today_tw:
            logger.debug(f"{symbol} yfinance 最新收盤 {last_date_tw}，今日 {today_tw}，資料未更新，跳過")
            return None

        _price_cache[symbol] = {"price": price, "time": now}
        return price
    except Exception as e:
        logger.warning(f"取價失敗 {symbol}: {e}")
        return None


def fetch_signal_data(symbol: str, lookback_days: int = 250) -> Optional[pd.DataFrame]:
    """取得用於信號計算的歷史數據（本地 CSV 優先、yfinance 備援）"""
    now = time.time()
    cache_key = symbol

    # 1. 記憶體快取（120 秒內不重複取）
    if cache_key in _price_cache and "df" in _price_cache[cache_key]:
        cached = _price_cache[cache_key]
        if now - cached["time"] < CACHE_TTL:
            return cached["df"]

    tw_tz = pytz.timezone("Asia/Taipei")
    today_tw = datetime.now(tw_tz).date()

    # 2. 本地 CSV
    local_df = _load_local_csv(symbol)
    csv_last_date = None
    if local_df is not None and len(local_df) >= 50:
        last_idx = local_df.index[-1]
        csv_last_date = last_idx.date() if hasattr(last_idx, 'date') else pd.Timestamp(last_idx).date()
        if csv_last_date >= today_tw:
            # CSV 已有今日資料 → 直接使用，不需要再問 yfinance
            _update_price_cache(cache_key, local_df, now)
            logger.info(f"{symbol} 使用本地 CSV（{len(local_df)} 筆，最新 {csv_last_date}）")
            return local_df

    # 3. yfinance API
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=f"{lookback_days}d", interval="1d")
        if df.empty or len(df) < 50:
            # yfinance 也失敗 → fallback 到本地 CSV
            if local_df is not None and len(local_df) >= 50:
                _update_price_cache(cache_key, local_df, now)
                return local_df
            return None

        df.columns = [c.lower() for c in df.columns]
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        df = df[df['volume'] > 0]

        yf_last = df.index[-1]
        yf_last_date = yf_last.date() if hasattr(yf_last, 'date') else pd.Timestamp(yf_last).date()

        # 核心防護：yfinance 回傳的最新日期比本地 CSV 舊 → 用 CSV
        if csv_last_date is not None and yf_last_date < csv_last_date:
            logger.warning(f"{symbol} yfinance 最新 {yf_last_date} 比本地 CSV {csv_last_date} 舊，使用本地 CSV")
            _update_price_cache(cache_key, local_df, now)
            return local_df

        _update_price_cache(cache_key, df, now)

        # 若 yfinance 有更新的資料 → 同步更新本地 CSV（走勢圖也受惠）
        if csv_last_date is None or yf_last_date > csv_last_date:
            _save_local_csv(symbol, df)

        return df
    except Exception as e:
        logger.warning(f"取數據失敗 {symbol}: {e}")
        # yfinance 例外 → fallback 到本地 CSV
        if local_df is not None and len(local_df) >= 50:
            _update_price_cache(cache_key, local_df, now)
            return local_df
        return None


def _update_price_cache(symbol: str, df: pd.DataFrame, now: float):
    """更新記憶體快取"""
    if symbol not in _price_cache:
        _price_cache[symbol] = {}
    _price_cache[symbol]["df"] = df
    _price_cache[symbol]["price"] = float(df['close'].iloc[-1])
    _price_cache[symbol]["time"] = now


def get_current_price(symbol: str) -> Optional[float]:
    """統一取價函式（供 status 端點使用）

    多來源比較日期，取最新的收盤價：
    1. 記憶體快取（當前 session auto_trader 已計算）
    2. last_prices.json（上次 auto_trader 確認的收盤價）
    3. 本地 CSV 最後收盤
    4. yfinance API（有日期保護，最後手段）
    """
    best_date = None
    best_price = None

    # Source 1: Memory cache df
    cached = _price_cache.get(symbol, {})
    df = cached.get("df")
    if df is not None and not df.empty:
        last_idx = df.index[-1]
        d = last_idx.date() if hasattr(last_idx, 'date') else pd.Timestamp(last_idx).date()
        best_date, best_price = d, float(df['close'].iloc[-1])

    # Source 2: Persistent last prices（auto_trader 確認過的）
    lp = _last_prices.get(symbol)
    if lp:
        try:
            d = datetime.strptime(lp["date"], "%Y-%m-%d").date()
            if best_date is None or d > best_date:
                best_date, best_price = d, lp["price"]
        except Exception:
            pass

    # Source 3: Local CSV
    local_df = _load_local_csv(symbol)
    if local_df is not None and not local_df.empty:
        last_idx = local_df.index[-1]
        d = last_idx.date() if hasattr(last_idx, 'date') else pd.Timestamp(last_idx).date()
        if best_date is None or d > best_date:
            best_date, best_price = d, float(local_df['close'].iloc[-1])

    if best_price is not None:
        return best_price

    # Source 4: yfinance（最後手段，有日期保護）
    return fetch_latest_price(symbol)


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
            "layer_modifiers": signal.layer_modifiers,
            "layer_reasons": [m.reason for m in signal.layer_modifiers if m.reason],
            "summary": signal.summary(),
        }
    except Exception as e:
        print(f"  ⚠️ 信號計算錯誤 {symbol}: {e}")
        return None


def compute_composite_score(symbol: str, sig: dict) -> Optional[float]:
    """
    計算五維綜合分數（與超選/四面分析一致的加權平均）

    從信號計算結果中的 layer_modifiers 提取各維度分數，
    按產業權重加總。用於買入前的品質門檻檢查。

    Returns:
        綜合分數 (0-100)，或 None（資料不足）
    """
    sector = get_symbol_sector(symbol)
    weights = SECTOR_COMPOSITE_WEIGHTS.get(sector, SECTOR_COMPOSITE_WEIGHTS["default"])

    scores = {}
    # 技術面：原始信號分數（未經 layer 修正）
    scores["technical"] = sig.get("raw_buy_score", sig.get("buy_score", 50))

    # 從 layer_modifiers 提取各層分數
    for mod in sig.get("layer_modifiers", []):
        if mod.layer_name == "regime":
            regime_scores_map = {
                "強勢多頭": 90, "多頭": 75, "底部轉強": 70,
                "盤整": 50, "高檔轉折": 25, "空頭": 15,
            }
            scores["regime"] = regime_scores_map.get(mod.regime, 50)
            # 傳產 Regime Veto-Only
            if sector == "traditional" and mod.regime in ("強勢多頭", "多頭"):
                scores["regime"] = min(scores["regime"], 60)
        elif mod.layer_name == "chipflow":
            scores["chipflow"] = mod.details.get("buy_score", 50)
        elif mod.layer_name == "fundamental":
            scores["fundamental"] = mod.details.get("buy_score", 50)
        elif mod.layer_name == "sentiment":
            scores["sentiment"] = mod.details.get("buy_score") if mod.details.get("buy_score") is not None else None

    # 加權平均（跳過無資料的維度）
    valid = [(scores.get(k, 50), w) for k, w in weights.items() if scores.get(k) is not None]
    if not valid:
        return None
    total_w = sum(w for _, w in valid)
    composite = sum(s * w for s, w in valid) / total_w
    return round(composite, 1)


# ── 單一類股交易循環 ──

def build_layers(strategy: dict) -> list:
    """根據策略配置建立分析層"""
    layers_config = strategy.get("layers", {
        "regime": {"enabled": True},
        "fundamental": {"enabled": True},
        "sentiment": {"enabled": True},
        "chipflow": {"enabled": True},
    })
    layers = []

    # Regime layer（預設啟用）
    regime_cfg = layers_config.get("regime", {"enabled": True})
    if regime_cfg.get("enabled", True):
        layers.append(RegimeLayer(enabled=True))

    # Fundamental layer（預設啟用）
    fund_cfg = layers_config.get("fundamental", {"enabled": True})
    if fund_cfg.get("enabled", True):
        layers.append(FundamentalLayer(enabled=True))

    # Sentiment layer（預設啟用）
    sent_cfg = layers_config.get("sentiment", {"enabled": True})
    if sent_cfg.get("enabled", True):
        layers.append(SentimentLayer(enabled=True))

    # ChipFlow layer（籌碼面，預設啟用）
    chip_cfg = layers_config.get("chipflow", {"enabled": True})
    if chip_cfg.get("enabled", True):
        layers.append(ChipFlowLayer(enabled=True))

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
    price_dates = {}  # symbol → 價格的實際交易日期

    for symbol in manager.state.get("stocks", []):
        # 1. 取得數據
        df = fetch_signal_data(symbol)
        if df is None:
            continue

        price = float(df['close'].iloc[-1])
        current_prices[symbol] = price
        # 記錄價格的實際日期（避免用舊日期覆蓋新價格）
        last_idx = df.index[-1]
        price_dates[symbol] = (last_idx.strftime("%Y-%m-%d")
                               if hasattr(last_idx, 'strftime')
                               else str(last_idx)[:10])

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

        # 4. 計算五維綜合分數（品質門檻）
        composite = compute_composite_score(symbol, sig)
        comp_tag = f" 綜合{composite:.0f}" if composite is not None else ""

        # 5. 信號交易（雙軌制：信號分數=時機 + 綜合分數=品質）
        regime_tag = f" [{sig['regime']}]" if sig.get("regime") else ""
        if hold and hold["qty"] > 0:
            # 已持倉 → 只看賣出信號（賣出不受綜合分數限制）
            if sig["direction"] == "SELL" and sig["confidence"] >= sell_th:
                desc = f"賣出信號 (技術{sig['confidence']:.0f},{comp_tag}, {sig['signal_level']}){regime_tag}"
                manager.execute_trade(symbol, "SELL", price, desc)
        else:
            # 無持倉 → 買入需同時滿足：信號達標 + 綜合 ≥ 50
            if sig["direction"] == "BUY" and sig["confidence"] >= buy_th:
                if composite is not None and composite < 50:
                    print(f"  [{manager.sector_name}] {symbol} 信號達標({sig['confidence']:.0f}分)"
                          f"但綜合分數不足({composite:.0f}<50)，跳過買入")
                    continue
                desc = f"買入信號 (技術{sig['confidence']:.0f},{comp_tag}, {sig['signal_level']}){regime_tag}"
                ratio = strategy.get("buy_ratio", 0.20)
                manager.execute_trade(symbol, "BUY", price, desc, ratio=ratio)

    # 5. 記錄權益 + 持久化最新價格（帶實際交易日期，避免舊價覆蓋新價）
    manager.record_equity(current_prices)
    if current_prices:
        _save_last_prices(current_prices, price_dates)


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

    @staticmethod
    def _is_tw_market_open() -> bool:
        """判斷現在是否為台股交易時段（週一～週五 08:30～13:35）"""
        tw_tz = pytz.timezone("Asia/Taipei")
        now_tw = datetime.now(tw_tz)
        weekday = now_tw.weekday()  # 0=週一 ... 6=週日
        if weekday >= 5:  # 週六、週日
            return False
        t = now_tw.hour * 60 + now_tw.minute  # 轉換為分鐘數
        # 08:30 = 510, 13:35 = 815（收盤後留 5 分鐘緩衝）
        return 510 <= t <= 815

    def _run_once(self):
        """執行一輪所有類股檢查"""
        self.last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not self._is_tw_market_open():
            return

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
