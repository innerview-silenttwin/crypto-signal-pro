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

import numpy as np
import pandas as pd
import pytz
import requests
from quote_provider import get_quote_provider

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from sector_trader import (
    get_all_managers, SectorTradingManager,
    SECTOR_STOCKS, SECTOR_IDS,
)
from layers import RegimeLayer, FundamentalLayer, SentimentLayer, ChipFlowLayer, LayerRegistry
from screener import get_symbol_sector, SECTOR_COMPOSITE_WEIGHTS
from brokers import market_hours
from brokers.factory import build_setup


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
        hist = get_quote_provider().get_history(symbol, period_days=5, interval="1d")
        if hist is None or hist.empty:
            return None

        # 去除 NaN 後取最後一筆
        valid = hist['close'].dropna()
        if valid.empty:
            return None

        last_date = valid.index[-1]
        price = float(valid.iloc[-1])

        # 若最新收盤日期 < 今日台灣日期，代表資料尚未更新
        # 回傳 None，讓呼叫端改用買入均價，避免跨日比較產生假損益
        tw_tz = pytz.timezone("Asia/Taipei")
        today_tw = datetime.now(tw_tz).date()
        last_date_tw = last_date.astimezone(tw_tz).date() if hasattr(last_date, 'astimezone') else last_date.date()

        if last_date_tw < today_tw:
            logger.debug(f"{symbol} 最新收盤 {last_date_tw}，今日 {today_tw}，資料未更新，跳過")
            return None

        _price_cache[symbol] = {"price": price, "time": now}
        return price
    except Exception as e:
        logger.warning(f"取價失敗 {symbol}: {e}")
        return None


# 1m 即時價快取（30 秒，避免單輪掃描重複呼叫）
_live_price_cache: Dict[str, dict] = {}
LIVE_PRICE_TTL = 30

# 除權息參考價（per-symbol 當日快取）；除息日昨收=除息前價，不能當 ±10%/漲停基準
_FINMIND = "https://api.finmindtrade.com/api/v4/data"
_div_ref_cache: Dict[str, dict] = {}


def _ex_dividend_ref(symbol: str, prev_date: str, cur_date: str) -> Optional[float]:
    """若最新交易日相對前一日之間發生除權息（含遇假日順延），回除息參考價(after_price)。

    否則 None。用途：除息生效日「昨收」是除息前價（偏高），會讓 ±10% 合理性防護與
    漲停偵測誤判（例：3034 除息 542→參考519、實價467.5 對 542 為 -13.7% 被當異常）。
    以除息參考價當基準即可正確判斷。資料源 FinMind TaiwanStockDividendResult。
    """
    code = symbol.replace(".TWO", "").replace(".TW", "").strip()
    if not code.isdigit():
        return None
    c = _div_ref_cache.get(code)
    if not c or c["day"] != cur_date:
        try:
            start = (datetime.strptime(cur_date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
            j = requests.get(_FINMIND, params={"dataset": "TaiwanStockDividendResult",
                                               "data_id": code, "start_date": start}, timeout=8).json()
        except Exception as e:
            logger.debug("除息資料取得失敗 %s: %s", code, e)
            return None                                    # 失敗不快取、下次重試（勿 silent stale）
        if j.get("msg") != "success":
            return None
        c = _div_ref_cache[code] = {"day": cur_date, "records": j.get("data") or []}
    for rec in c["records"]:
        exd = rec.get("date")
        if exd and prev_date < exd <= cur_date:           # 除息生效落在(前一交易日, 最新交易日]
            try:
                ref = float(rec.get("after_price") or 0)
                if ref > 0:
                    return ref
            except (TypeError, ValueError):
                continue
    return None


def fetch_live_price(symbol: str, prev_close: Optional[float] = None,
                     alt_ref_fn=None) -> Optional[float]:
    """取得盤中即時成交價（1m K 最後一根 close）。

    yfinance daily candle 在盤中會延遲/快取，漲停或低成交標的特別容易抓到舊值。
    1m K 線是真實逐筆成交，用來作交易執行價較可靠。

    若提供 prev_close，會檢查價格是否在昨收 ±10%（台股漲跌停限制）內，超出代表 1m 資料異常。
    alt_ref_fn：僅在價格「超出 prev_close ±10%」時才呼叫（回除權息參考價）——除息日昨收偏高
    會誤判正常價為異常，用參考價再驗一次；懶惰呼叫確保正常情況零額外網路。
    """
    now = time.time()
    cached = _live_price_cache.get(symbol)
    if cached and now - cached["time"] < LIVE_PRICE_TTL:
        return cached["price"]

    try:
        df = get_quote_provider().get_history(symbol, period_days=1, interval="1m")
        if df is None or df.empty:
            return None
        df = df.dropna(subset=['close'])
        if df.empty:
            return None
        price = float(df['close'].iloc[-1])

        # 合理性檢查：±10% 漲跌停範圍
        if prev_close and prev_close > 0 and not (prev_close * 0.89 <= price <= prev_close * 1.11):
            alt = alt_ref_fn() if alt_ref_fn else None    # 只有異常時才查除息（省網路）
            if not (alt and alt > 0 and alt * 0.89 <= price <= alt * 1.11):
                base = alt if alt else prev_close
                logger.warning(
                    f"{symbol} 1m 即時價 {price:.2f} 超出基準 {base:.2f} ±10%，疑似異常"
                )
                return None
            logger.info(f"{symbol} 除權息參考價 {alt:.2f}：即時價 {price:.2f} 屬正常波動（昨收 {prev_close:.2f}）")

        _live_price_cache[symbol] = {"price": price, "time": now}
        return price
    except Exception as e:
        logger.warning(f"取即時價失敗 {symbol}: {e}")
        return None


# ── TAIEX 大盤 regime（給 F3 entry filter 用）──────────────────────
# 研究背景：scripts/backtest_exits/REPORT.md
# 「持續盤整」段 134 筆全策略漏血 -107K~-143K，主因是 TAIEX neutral 時
# 低分（buy_score 40-50）進場 → 75 筆吃掉 70% 漏血。
# F3：TAIEX neutral 時把 buy 門檻提到 50。回測 +9.9pp 全期報酬 / +24pp MDD。

_taiex_regime_cache = {"time": 0.0, "regime": None}
_TAIEX_CACHE_TTL = 600  # 10 分鐘（每個 process_sector 共用）


def fetch_taiex_regime() -> str:
    """取得 TAIEX 大盤 regime: bull / neutral / bear

    判定（與回測 scripts/backtest_exits 一致）：
      bull   = TAIEX close > MA200 且 MA50 > MA200
      bear   = TAIEX close < MA200 且 MA50 < MA200
      其它   = neutral（盤整）
    """
    now = time.time()
    cached = _taiex_regime_cache.get("regime")
    if cached is not None and now - _taiex_regime_cache["time"] < _TAIEX_CACHE_TTL:
        return cached

    try:
        df = fetch_signal_data("^TWII", lookback_days=300)
        if df is None or len(df) < 200:
            return "neutral"  # 資料不足保守處理（會啟用 F3 嚴格門檻）
        close = float(df['close'].iloc[-1])
        ma50 = float(df['close'].rolling(50).mean().iloc[-1])
        ma200 = float(df['close'].rolling(200).mean().iloc[-1])
        if pd.isna(ma200) or ma200 <= 0:
            regime = "neutral"
        elif close > ma200 and ma50 > ma200:
            regime = "bull"
        elif close < ma200 and ma50 < ma200:
            regime = "bear"
        else:
            regime = "neutral"

        # 只在 regime 變化或首次計算時 log（避免洗版）
        prev = _taiex_regime_cache.get("regime")
        if prev != regime:
            logger.info(f"TAIEX regime: {prev or '(初始)'} → {regime} "
                        f"(close={close:.0f}, MA50={ma50:.0f}, MA200={ma200:.0f})")
        _taiex_regime_cache["time"] = now
        _taiex_regime_cache["regime"] = regime
        return regime
    except Exception as e:
        logger.warning(f"fetch_taiex_regime 失敗: {e}，保守用 neutral")
        return "neutral"


def _expected_latest_trading_day_date():
    """回傳「上個已收盤交易日」的 date 物件（含週末跳過、14:30 前算昨日）。

    與 main.py:latest_closed_tw_trading_day() 邏輯一致；內聯避免反向 import 觸發循環依賴。
    """
    tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tz)
    candidate = now.date()
    today_close = now.replace(hour=14, minute=30, second=0, microsecond=0)
    if now.weekday() < 5 and now >= today_close:
        return candidate
    candidate = candidate - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate - timedelta(days=1)
    return candidate


def fetch_signal_data(symbol: str, lookback_days: int = 250) -> Optional[pd.DataFrame]:
    """取得用於信號計算的歷史數據（quote_provider only，無 CSV 兜底）。

    2026-06-03 重寫：拔掉本地 CSV 兜底。底線「不能用舊資料判斷觸發交易」要求：
    寧可 skip 該輪也不能用 stale CSV 算信號。CSV 留給「顯示 path」用，
    交易 path 完全靠即時 quote_provider（sinopac 主、yfinance 備援由 provider 內部處理）。

    流程：
      1. memory cache 120 秒 → return（避免同輪重複查）
      2. quote_provider → 驗 freshness（最新日期 ≥ 上個交易日 OR == 今日 partial）
      3. quote_provider 失敗或回 stale → return None → process_sector skip 該股該輪
    """
    now = time.time()
    cache_key = symbol

    # 1. 記憶體快取
    if cache_key in _price_cache and "df" in _price_cache[cache_key]:
        cached = _price_cache[cache_key]
        if now - cached["time"] < CACHE_TTL:
            return cached["df"]

    tw_tz = pytz.timezone("Asia/Taipei")
    today_tw = datetime.now(tw_tz).date()

    # 2. Quote provider
    try:
        df = get_quote_provider().get_history(symbol, period_days=lookback_days, interval="1d")
        if df is None or df.empty or len(df) < 50:
            logger.warning(f"{symbol} quote_provider 無資料；skip 該輪不交易")
            return None

        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        df = df[df['volume'] > 0]
        if len(df) < 50:
            logger.warning(f"{symbol} quote_provider 有效資料 < 50 筆；skip 該輪不交易")
            return None

        # ── Freshness 守則（底線：交易不准用 stale 資料）──
        last_idx = df.index[-1]
        last_date = last_idx.date() if hasattr(last_idx, 'date') else pd.Timestamp(last_idx).date()
        expected_latest = _expected_latest_trading_day_date()

        # 指數類（^TWII / ^TWO 等）放寬：允許「上一交易日」資料
        # Why: TAIEX 等指數在台股 14:30 收盤後，yfinance 可能要到隔天才更新到當日線。
        # 個股 yfinance 盤中會有 partial snapshot，指數通常沒有。
        # 對指數判 stale 太嚴 → fetch_taiex_regime() return "neutral" → F3 把全 sector
        # buy_th 拉高到 50 → 大量 BUY 訊號被擋。
        is_index = symbol.startswith("^")
        if is_index:
            # 指數：last_date < (expected_latest - 1 個交易日) 才算 stale
            stale_threshold = expected_latest - timedelta(days=3)  # 給週末緩衝
            if last_date < stale_threshold:
                logger.warning(
                    f"{symbol} (指數) quote 回 last={last_date} < threshold={stale_threshold}; skip"
                )
                return None
        else:
            # 個股：允許「今日 partial」(last_date == today)；其它 < expected_latest 視為 stale
            if last_date < expected_latest and last_date != today_tw:
                logger.warning(
                    f"{symbol} quote_provider 回 stale 資料 last={last_date} < expected={expected_latest}; "
                    f"skip 該輪不交易（不再 fallback CSV，避免用舊資料觸發交易）"
                )
                return None

        _update_price_cache(cache_key, df, now)
        return df

    except Exception as e:
        logger.warning(f"{symbol} 取數據失敗 {e.__class__.__name__}: {e}; skip 該輪不交易")
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


# ── Per-symbol BUY filter（5.5 年回測證據明確的調整）──
# 詳見 backend/backtest_results/filter_backtest_20260608_230729.md
# 收錄條件：A_volume vs baseline 報酬改善 ≥ 30pp，且 MDD 同向不惡化。
# 注意：回測 baseline 用 BUY_TH=40 + 無 composite + 無 F3 + 無 pullback；
# production 已有 composite≥50 + effective_buy_th=50 等 gate，實際改善幅度
# 可能小於回測數字（為上限）。後續 Phase 2 會用 production-equivalent baseline 重跑。
# Key 必須與 production state["stocks"] 一致（帶 .TW 後綴），否則 .get() 永遠 miss → 死碼。
SYMBOL_BUY_FILTER: Dict[str, str] = {
    "2382.TW": "A_volume",   # 廣達 baseline +78.7% → A_volume +146.1% (+67.3pp)，MDD 41→35
    "2454.TW": "A_volume",   # 聯發科 baseline +11.0% → A_volume +53.4% (+42.4pp)，MDD 49→38
                             # 證據單一 baseline，Phase 2 production-equivalent rerun 後再二次確認
    "2881.TW": "A_volume",   # 富邦金 baseline +5.1% → A_volume +39.1% (+34.0pp)，MDD 30→16
}
# 觀察名單（證據邊界，暫不列入；等 Phase 2 重跑再決定）：
#   2317.TW 鴻海：return +118→+118 持平，但 MDD 33→20 (-13pp)。屬「MDD 改善」型，需新標準
#   2882.TW 國泰金：return +0.5pp、MDD -5pp，太弱
#   2891.TW 中信金：A_volume 報酬比 baseline 差 -10pp，雖 MDD -12pp 但顯然 baseline 更優


def _filter_a_volume_check(df: pd.DataFrame, ratio_th: float = 1.5) -> tuple[bool, float]:
    """A 量能 filter：當日 volume / 前 20 日均量 >= ratio_th。

    回測同 backend/run_filter_backtest.py filter_volume(idx, df)。
    資料不足（< 21 根 K）或均量 = 0 → 不阻擋（return True）以免新上市股被吃掉。
    """
    if len(df) < 21:
        return True, 0.0
    vol = float(df["volume"].iloc[-1])
    avg20 = float(df["volume"].iloc[-21:-1].mean())
    if avg20 <= 0:
        return True, 0.0
    vol_ratio = vol / avg20
    return vol_ratio >= ratio_th, vol_ratio


def should_pass_symbol_filter(symbol: str, df: pd.DataFrame) -> tuple[bool, str]:
    """Per-symbol BUY entry filter。未列入 SYMBOL_BUY_FILTER 的 symbol 一律通過。

    Returns:
        (是否通過, 顯示用描述). 描述空字串表示 symbol 未受 filter 約束。
    """
    name = SYMBOL_BUY_FILTER.get(symbol)
    if not name:
        return True, ""
    if name == "A_volume":
        ok, vol_ratio = _filter_a_volume_check(df)
        if ok:
            return True, f"A_volume過(vol_ratio={vol_ratio:.2f})"
        return False, f"A_volume擋(vol_ratio={vol_ratio:.2f}<1.5)"
    # 未知 filter 名稱 → 不阻擋（避免設定錯字把所有交易擋掉）
    return True, f"unknown_filter:{name}"


# ── 強勢拉回偵測（高檔拉回加碼點）──

def is_strong_pullback(sig: dict) -> tuple[bool, dict]:
    """
    判斷是否為「強勢拉回」訊號 — 高檔轉折下的主力洗盤再攻

    三條件同時成立：
    1. regime == 高檔轉折（120日相對高檔 + K線/量價警示）
    2. 投信連買 ≥ 3 天（機構仍在吸籌）
    3. 原始技術買分 raw_buy_score ≥ 40（動能未破壞）

    回測（2026-01 ~ 04，n=31）：+10d 平均 +18.36%、勝率 82.6%
    用途：繞過 RegimeLayer 的 veto_buy，識別強勢股拉回的加碼甜蜜點

    Returns:
        (是否觸發, 細節 dict)
    """
    if sig.get("regime") != "高檔轉折":
        return False, {}
    raw_buy = sig.get("raw_buy_score", 0)
    if raw_buy < 40:
        return False, {}

    for mod in sig.get("layer_modifiers", []):
        if mod.layer_name == "chipflow":
            trust = mod.details.get("trust_consec_buy", 0)
            if trust >= 3:
                return True, {
                    "raw_buy_score": raw_buy,
                    "trust_consec_buy": trust,
                    "foreign_consec_buy": mod.details.get("foreign_consec_buy", 0),
                }
    return False, {}


# ── 趨勢破壞型賣出（價量型出場觸發）──

def is_trend_break_sell(df: pd.DataFrame, sig: dict,
                        hold: Optional[dict] = None) -> tuple[bool, dict]:
    """
    判斷是否觸發「趨勢破壞型賣出」（補強 RegimeLayer 對中段破線的遲鈍）

    觸發條件（S1 OR S9，且 regime ∈ {高檔轉折, 盤整, 多頭}）：
    - S1: 從持倉以來最高價回落 ≥ 3×ATR（個股級 trailing stop）
          fallback：舊持倉無 highest_since_entry → 用 max(avg_price, 近20日高)
    - S9: 連續 3 黑 K + 收盤跌破 20MA（K 棒型態破壞）

    研究背景：scripts/backtest_exits/REPORT.md
    回測 S1 取代原 S8（從 20 日高點 3×ATR）+ F3 entry filter 組合：
    全期 +63.5% / MDD -59.6% (vs 原 baseline +45.6% / -84.8%)。

    「強勢多頭」不納入（樣本不足且本身已有 sell ×0.5 防護）。

    Args:
        df: 個股 OHLCV（含技術指標）
        sig: 信號 dict（含 regime）
        hold: 持倉狀態 dict，含 highest_since_entry / avg_price

    Returns:
        (是否觸發, 細節 dict)
    """
    regime = sig.get("regime")
    if regime not in ("高檔轉折", "盤整", "多頭"):
        return False, {}

    if df is None or len(df) < 60:
        return False, {}

    closes = df['close'].values
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    n = len(df)
    i = n - 1

    c = float(closes[i])
    ma20 = float(pd.Series(closes).rolling(20).mean().iloc[i])
    if ma20 <= 0:
        return False, {}

    # S9: 連續 3 黑 K + 收盤 < 20MA
    s9_red3 = all(closes[i - k] < opens[i - k] for k in range(3))
    s9 = s9_red3 and c < ma20

    # S1: 從持倉以來最高價回落 ≥ 3×ATR
    # 取 highest_since_entry；舊資料 fallback 到 max(avg_price, 近20日收盤高)
    if hold and hold.get("highest_since_entry"):
        highest = float(hold["highest_since_entry"])
    elif hold and hold.get("avg_price"):
        # backward compat：用 max(avg_price, 近20日收盤高) 當保守 proxy
        highest = max(float(hold["avg_price"]),
                      float(np.max(closes[max(0, i - 19):i + 1])))
    else:
        highest = float(np.max(highs[max(0, i - 19):i + 1]))

    # 簡化 ATR(14)
    tr_list = []
    for k in range(14):
        h = highs[i - k]
        l = lows[i - k]
        pc = closes[i - k - 1] if i - k - 1 >= 0 else closes[i - k]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr14 = float(np.mean(tr_list))
    s1 = atr14 > 0 and (highest - c) >= 3.0 * atr14

    if not (s1 or s9):
        return False, {}

    triggered = []
    if s9:
        triggered.append("S9連3黑破20MA")
    if s1:
        triggered.append(f"S1從持倉高{highest:.1f}跌{(highest-c)/atr14:.1f}×ATR")

    return True, {
        "triggers": triggered,
        "regime": regime,
        "ma20": round(ma20, 2),
        "highest_since_entry": round(highest, 2),
        "atr14": round(atr14, 2),
    }


# ── 超跌反彈型買入（價量型進場觸發）──

def is_oversold_rebound(df: pd.DataFrame, sig: dict) -> tuple[bool, dict]:
    """
    判斷是否觸發「超跌反彈型買入」（補強空頭/底部轉強區的進場機會）

    觸發條件（B2 OR B5，且 regime ∈ {空頭, 底部轉強, 盤整}）：
    - B2: 連漲 3 日累計 ≥ 5%（持續性反彈）
    - B5: 單日漲 ≥ 4% + 量比 > 2（強勢突破）

    回測（2024-01 ~ 2026-04，76 檔）：
    - B2 在「空頭」: n=141, +10d=+3.83%, 勝率 62.4%
    - B2 在「底部轉強」: n=167, +10d=+2.23%, 勝率 58.7%
    - B5 在「空頭」: n=25, +10d=+3.31%, 勝率 60.0%

    用途：在空頭中抓反彈、在底部轉強區加強買入信號（繞過 RegimeLayer veto_buy）

    Returns:
        (是否觸發, 細節 dict)
    """
    regime = sig.get("regime")
    if regime not in ("空頭", "底部轉強", "盤整"):
        return False, {}

    if df is None or len(df) < 30:
        return False, {}

    closes = df['close'].values
    vols = df['volume'].values
    n = len(df)
    i = n - 1

    if i < 3:
        return False, {}

    # B2: 連漲 3 日累計 ≥ 5%
    u3 = (closes[i] / closes[i - 3] - 1) * 100
    b2 = u3 >= 5.0

    # B5: 單日漲 ≥ 4% + 量比 > 2
    daily_chg = (closes[i] / closes[i - 1] - 1) * 100
    vol_ma20 = float(pd.Series(vols).rolling(20).mean().iloc[i])
    vol_ratio = (vols[i] / vol_ma20) if vol_ma20 > 0 else 0
    b5 = daily_chg >= 4.0 and vol_ratio > 2.0

    if not (b2 or b5):
        return False, {}

    triggered = []
    if b2:
        triggered.append(f"B2連漲3日{u3:.1f}%")
    if b5:
        triggered.append(f"B5單日漲{daily_chg:.1f}%量比{vol_ratio:.1f}x")

    return True, {
        "triggers": triggered,
        "regime": regime,
        "consec_3d_pct": round(u3, 2),
        "daily_chg_pct": round(daily_chg, 2),
        "vol_ratio": round(vol_ratio, 2),
    }


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

    # F3 entry filter（見 scripts/backtest_exits/REPORT.md）
    # TAIEX 盤整時把 BUY 門檻提到 50，避免在橫盤期低分進場被洗。
    # log 由 fetch_taiex_regime 在 regime 變化時印一次；這裡只計算 effective_buy_th。
    taiex_regime = fetch_taiex_regime()
    effective_buy_th = max(buy_th, 50) if taiex_regime == "neutral" else buy_th

    # 建立分析層
    layers = build_layers(strategy)

    current_prices = {}
    price_dates = {}  # symbol → 價格的實際交易日期

    tw_tz = pytz.timezone("Asia/Taipei")
    now_tw = datetime.now(tw_tz)
    today_str = now_tw.strftime("%Y-%m-%d")
    # 統一台股交易時段判斷：09:00–13:30 平日，含節假日排除
    is_market_hours = market_hours.is_signal_window(now_tw)

    for symbol in manager.state.get("stocks", []):
        # 1. 取得歷史數據（信號計算用）
        df = fetch_signal_data(symbol)
        if df is None:
            # per-symbol 診斷：哪檔被跳過、哪條路徑卡住。用 info 級讓 prod log 隨時看得到
            # 真實狀況（資料源掛時會一次噴多檔，是預期的——那正是要觀察的訊號）。
            logger.info("[%s] %s SKIP: fetch_signal_data 回 None", manager.sector_name, symbol)
            continue

        # 2. 決定執行價
        # 盤中：優先用 1m K 即時價（避免 daily candle 延遲/快取問題）
        # 收盤後：用 daily close
        daily_price = float(df['close'].iloc[-1])
        prev_close = float(df['close'].iloc[-2]) if len(df) >= 2 else None
        # 除權息生效日「昨收」=除息前價（偏高），會讓 ±10% 合理性防護誤判正常價為異常。
        # 懶惰查除息參考價：只有價格真的超出昨收 ±10% 時才打 FinMind（正常情況零額外網路）。
        _ex_ref_fn = None
        if prev_close and len(df) >= 2:
            _pd, _cd = df.index[-2], df.index[-1]
            _pds = _pd.strftime("%Y-%m-%d") if hasattr(_pd, 'strftime') else str(_pd)[:10]
            _cds = _cd.strftime("%Y-%m-%d") if hasattr(_cd, 'strftime') else str(_cd)[:10]
            _ex_ref_fn = lambda: _ex_dividend_ref(symbol, _pds, _cds)  # noqa: E731
        price = daily_price
        live = None
        if is_market_hours:
            live = fetch_live_price(symbol, prev_close=prev_close, alt_ref_fn=_ex_ref_fn)
            if live is not None:
                price = live

        current_prices[symbol] = price
        last_idx = df.index[-1]
        price_date_str = (last_idx.strftime("%Y-%m-%d")
                          if hasattr(last_idx, 'strftime')
                          else str(last_idx)[:10])
        price_dates[symbol] = price_date_str

        # ── 價格日期守衛：daily 是非今日資料時 ──
        # 盤後：直接跳過
        # 盤中：必須有 1m 即時價當參考；1m 也失敗代表「全部資料都是昨日的」 → 跳過避免用昨收下單
        if price_date_str < today_str:
            if is_market_hours and live is None:
                logger.warning(
                    f"{symbol} 盤中 daily 仍為 {price_date_str}、1m 也抓不到，跳過避免用昨收下單"
                )
                continue
            if not is_market_hours:
                logger.warning(
                    f"{symbol} 價格日期 {price_date_str} 非今日 {today_str}，跳過交易"
                )
                continue

        # ── 漲停偵測：漲幅 ≥9.5% 不買入（實務上漲停板買不到） ──
        # 用 prev_close；除息日漲停會「低估」(基準偏高→算出漲幅偏小)屬罕見小邊際、不誤買為主
        _is_limit_up = False
        if prev_close and prev_close > 0 and price >= prev_close * 1.095:
            _is_limit_up = True
            logger.info(f"{symbol} 漲停板（{price:.1f} / 昨收 {prev_close:.1f}），跳過買入")

        # 3. 計算信號（含分析層修正）
        sig = compute_signal(df, weights, symbol,
                             layers=layers, sector_id=manager.sector_id)
        if sig is None:
            print(f"  [{manager.sector_name}] {symbol} SKIP: compute_signal 回 None")
            continue

        # Log regime info
        if sig.get("regime"):
            regime_reasons = sig.get("layer_reasons", [])
            reason_str = " | ".join(regime_reasons) if regime_reasons else ""
            print(f"  [{manager.sector_name}] {symbol} 盤勢:{sig['regime']} "
                  f"買:{sig['buy_score']:.0f}(原{sig['raw_buy_score']:.0f}) "
                  f"賣:{sig['sell_score']:.0f}(原{sig['raw_sell_score']:.0f}) "
                  f"{reason_str}")

        # 4. 檢查停損/停利（已持倉）
        hold = manager.state["holdings"].get(symbol)
        if hold and hold["qty"] > 0:
            # 更新 highest_since_entry（S1 trailing stop 基準，每日跟價更新）
            cur_high = float(hold.get("highest_since_entry") or hold["avg_price"])
            if price > cur_high:
                hold["highest_since_entry"] = round(price, 2)

            pnl_pct = (price - hold["avg_price"]) / hold["avg_price"] * 100

            if pnl_pct <= -stop_loss:
                manager.execute_trade(
                    symbol, "SELL", price,
                    f"停損觸發 ({pnl_pct:.1f}%)",
                    is_auto_stop=True,
                )
                continue
            elif pnl_pct >= take_profit:
                manager.execute_trade(
                    symbol, "SELL", price,
                    f"停利觸發 ({pnl_pct:.1f}%)",
                    is_auto_stop=True,
                )
                continue

        # 5. 計算五維綜合分數（品質門檻）
        composite = compute_composite_score(symbol, sig)
        comp_tag = f" 綜合{composite:.0f}" if composite is not None else ""

        # 6. 信號交易（雙軌制：信號分數=時機 + 綜合分數=品質）
        regime_tag = f" [{sig['regime']}]" if sig.get("regime") else ""
        if hold and hold["qty"] > 0:
            # 已持倉 → 賣出觸發兩條：標準信號 + 趨勢破壞型
            standard_sell = (sig["direction"] == "SELL" and sig["confidence"] >= sell_th)
            trend_break, tb_detail = is_trend_break_sell(df, sig, hold=hold)

            if standard_sell:
                desc = f"賣出信號 (技術{sig['confidence']:.0f},{comp_tag}, {sig['signal_level']}){regime_tag}"
                manager.execute_trade(symbol, "SELL", price, desc)
            elif trend_break:
                # 趨勢破壞型：在高檔轉折/盤整 regime 下，連3黑破20MA 或 從高跌3×ATR
                # B 守衛：只在持有 ≥ 1 個交易日才生效（避免「剛買進就被 trend_break 賣掉」的矛盾）
                # 用戶 2026-05-20 觀察：近 60 天 trend_break 訊號真破壞率僅 4%，
                # 強勢上漲 regime 下「跌深」多半是回檔而非反轉。
                # 但仍保留此邏輯，只是要求過夜（給市場一天驗證是真破壞還是假警報）。
                from datetime import datetime as _dt
                hold_time_str = hold.get("time", "")
                hold_overnight = False
                if hold_time_str:
                    try:
                        hold_dt = _dt.strptime(hold_time_str, "%Y-%m-%d %H:%M:%S")
                        # 持有超過 16 小時 = 至少跨夜
                        if (_dt.now() - hold_dt).total_seconds() >= 16 * 3600:
                            hold_overnight = True
                    except Exception:
                        # 解析失敗保守處理 — 允許執行（向下相容無 time 欄位的舊資料）
                        hold_overnight = True
                else:
                    hold_overnight = True  # 老資料沒記時間 → 不擋
                trig_str = "+".join(tb_detail.get("triggers", []))
                if hold_overnight:
                    desc = f"趨勢破壞賣出 ({trig_str}){regime_tag}"
                    manager.execute_trade(symbol, "SELL", price, desc)
                else:
                    logger.info(
                        f"{symbol} trend_break ({trig_str}) 但持有 < 16 小時，先觀察不賣"
                    )
        else:
            # 無持倉 → 買入需同時滿足：信號達標 + 綜合 ≥ 50
            # F3：TAIEX neutral 時用 effective_buy_th (≥50)，其它情境維持 sector buy_th

            # no_buy_symbols：該股在此 sector 被標為「只賣不買」（如 2317 屬電子代工、
            # 不該在 semiconductor 出現）。已有持倉走上面 SELL/停損分支，這裡只擋新進場。
            no_buy_set = set(manager.state.get("no_buy_symbols", []))
            if symbol in no_buy_set:
                print(f"  [{manager.sector_name}] {symbol} 在 no_buy 清單，跳過買入")
                continue

            standard_buy = (sig["direction"] == "BUY"
                            and sig["confidence"] >= effective_buy_th)
            pullback_buy, pb_detail = is_strong_pullback(sig)
            rebound_buy, rb_detail = is_oversold_rebound(df, sig)

            if _is_limit_up:
                continue

            if standard_buy or pullback_buy or rebound_buy:
                if composite is not None and composite < 50:
                    if pullback_buy and not standard_buy:
                        src = "強勢拉回"
                    elif rebound_buy and not standard_buy:
                        src = "超跌反彈"
                    else:
                        src = "信號達標"
                    print(f"  [{manager.sector_name}] {symbol} {src}"
                          f"但綜合分數不足({composite:.0f}<50)，跳過買入")
                    continue
                # Per-symbol filter 只針對 standard_buy 路徑。被擋下時若同時有
                # pullback/rebound 等籌碼面退路（已自帶 70% 倉位折扣），降級走退路；
                # 沒退路才 continue。避免「最有把握的 overlap case 反被全擋」的 bug。
                if standard_buy:
                    sf_ok, sf_detail = should_pass_symbol_filter(symbol, df)
                    if not sf_ok:
                        if pullback_buy or rebound_buy:
                            print(f"  [{manager.sector_name}] {symbol} {sf_detail}，"
                                  f"standard 路徑跳過，落 pullback/rebound 退路")
                            standard_buy = False  # 走下方 pullback / rebound 70% 倉位分支
                        else:
                            print(f"  [{manager.sector_name}] {symbol} {sf_detail}，跳過買入")
                            continue
                if pullback_buy and not standard_buy:
                    # 強勢拉回加碼點：使用較小倉位（70%）防護單次失誤
                    desc = (f"強勢拉回加碼點 (原買分{pb_detail['raw_buy_score']:.0f}, "
                            f"投信連買{pb_detail['trust_consec_buy']}天, "
                            f"外資連買{pb_detail['foreign_consec_buy']}天){regime_tag}")
                    ratio = strategy.get("buy_ratio", 0.20) * 0.7
                elif rebound_buy and not standard_buy:
                    # 超跌反彈進場：保守倉位 70%
                    trig_str = "+".join(rb_detail.get("triggers", []))
                    desc = f"超跌反彈進場 ({trig_str}){regime_tag}"
                    ratio = strategy.get("buy_ratio", 0.20) * 0.7
                else:
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
        self._broker_setup = None
        self._broker_inited = False

    def _ensure_broker_setup(self) -> None:
        """啟動時把 broker / risk_gate / state_store 注入到每個 manager。

        延遲到 start 後第一次執行才呼叫，這樣測試可以單獨 import 模組而不需要 .env。
        """
        if self._broker_inited:
            return
        managers = get_all_managers()
        sector_ids = list(managers.keys())

        def _equity_provider(sid: str):
            m = managers.get(sid)
            if m is None:
                return (0.0, 0.0)
            return (m.get_equity(), float(m.state.get("balance", 0.0) or 0.0))

        def _position_provider(sid: str, symbol: str):
            m = managers.get(sid)
            if m is None:
                return {}
            return m.get_position(symbol)

        def _initial_balance_provider(sid: str):
            m = managers.get(sid)
            if m is None:
                return 0.0
            return float(m.state.get("initial_balance", 0.0) or 0.0)

        try:
            self._broker_setup = build_setup(
                sector_ids=sector_ids,
                equity_provider=_equity_provider,
                position_provider=_position_provider,
                initial_balance_provider=_initial_balance_provider,
            )
        except Exception as e:
            logger.exception("broker setup failed (%s); 全部 sector 走 VirtualBroker 預設", e.__class__.__name__)
            self._broker_inited = True
            # build_setup 整個失敗 = 最嚴重的降級（所有 sector 都沒 broker）。
            # 這條路徑 _broker_setup 仍是 None、per-sector 偵測攔不到、21:00 報告也會 silent，
            # 所以這裡直接發一封 catastrophic 告警，避免「最壞情況反而無聲」。
            self._alert_broker_setup_failed(e)
            return

        for sid, mgr in managers.items():
            broker = self._broker_setup.brokers_by_sector.get(sid)
            mgr.attach_broker(
                broker=broker,
                risk_gate=self._broker_setup.risk_gate,
                state_store=self._broker_setup.state_store,
            )
            logger.info("sector %s broker=%s", sid, broker.name if broker else "default")
        self._broker_inited = True
        self._alert_broker_degradation()

    def _alert_broker_degradation(self) -> None:
        """broker init 後，若「期望永豐、實際降級虛擬」就發一次性 Telegram 告警。

        典型情境：永豐 sim 503 SystemMaintenance（如 2026-06-24），factory silent
        fallback VirtualBroker、整天紙上交易。過去使用者只能靠肉眼看交易標籤發現；
        這裡主動告警，補上 heartbeat「只報健康、不報 broker 降級」的盲點。

        附註：目前 broker 是 startup 一次性 build，恢復後不會自動切回，所以這裡也只會
        在每次 service restart 後告警一次（待 broker init retry 實作後再做恢復通知）。
        """
        try:
            from brokers.factory import detect_broker_degradation
            from notifier import send_telegram

            setup = self._broker_setup
            if setup is None:
                return
            status = detect_broker_degradation(setup.brokers_by_sector)
            degraded = status.get("degraded") or []
            if not degraded:
                return
            ok = status.get("ok") or []
            lines = [
                "⚠️ <b>Broker 降級告警</b>",
                "期望：永豐 simulation；實際：部分 sector fallback 虛擬交易",
                f"\U0001f4c9 降級虛擬：{', '.join(degraded)}",
            ]
            if ok:
                lines.append(f"\U0001f3e6 仍走永豐：{', '.join(ok)}")
            lines.append("可能原因：永豐 login 失敗（如 503 SystemMaintenance）/ 連線異常。")
            lines.append("今日這些 sector 的交易為紙上單、未送永豐。下次 service restart 會重試。")
            send_telegram("\n".join(lines))
            logger.warning("broker 降級告警已發送：degraded=%s ok=%s", degraded, ok)
        except Exception:
            # 告警失敗絕不可影響交易引擎啟動
            logger.exception("_alert_broker_degradation failed (non-fatal)")

    def _alert_broker_setup_failed(self, exc: Exception) -> None:
        """build_setup 整個失敗時發 catastrophic Telegram 告警。

        只在 BROKER_MODE=sinopac 時告警（virtual 模式整個失敗另有預設處理、非異常）。
        """
        try:
            if os.environ.get("BROKER_MODE", "virtual").strip().lower() != "sinopac":
                return
            from notifier import send_telegram
            send_telegram(
                "\U0001f6a8 <b>Broker 初始化整個失敗</b>\n"
                f"錯誤：{exc.__class__.__name__}\n"
                "所有 sector 退回預設、今日交易未送永豐。\n"
                "請檢查 logs/ 與 .env / broker_config.yaml；下次 service restart 會重試。"
            )
            logger.warning("broker setup 失敗 catastrophic 告警已發送")
        except Exception:
            logger.exception("_alert_broker_setup_failed failed (non-fatal)")

    def start(self):
        if self._running:
            return False
        self._ensure_broker_setup()
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
        """判斷現在是否為台股可下單時段。已統一用 market_hours.is_signal_window。

        signal_window: 09:00–13:30 平日（含節假日排除）
        """
        return market_hours.is_signal_window()

    def _reconcile_brokers(self) -> None:
        """同步所有 broker 的 in-flight 訂單。重啟後或 partial fill 兜底用。
        順便清理 state_store 內超過 2× fill_timeout 還沒回的 stale pending（broker submit hang 造成）。
        """
        if not self._broker_setup:
            return
        for sid, broker in self._broker_setup.brokers_by_sector.items():
            try:
                completed = broker.reconcile()
                if completed:
                    logger.info("[reconcile] %s: %d 筆 in-flight 訂單已完成", sid, len(completed))
            except Exception as e:
                logger.warning("[reconcile] %s failed: %s", sid, e.__class__.__name__)

        # stale pending 清理（跨 sector 統一掃）
        store = self._broker_setup.state_store
        # 取 max fill_timeout × 2 + 30 秒緩衝 作為 stale 門檻
        # 2026-06-01：fill_timeout 主常量從 30s → 60s，fallback 同步調
        max_fill_timeout = 60  # 預設；若 broker 有暴露 fill_timeout_s 可動態查
        for b in self._broker_setup.brokers_by_sector.values():
            t = getattr(b, "_fill_timeout_s", 0)
            if t and t > max_fill_timeout:
                max_fill_timeout = t
        stale_threshold = max_fill_timeout * 2 + 30
        now_ts = time.time()
        try:
            for p in list(store.list_pending()):
                age = now_ts - (p.submitted_at or now_ts)
                if age > stale_threshold:
                    logger.warning(
                        "[reconcile] stale pending %s (%s %s %d股 @%.2f) age=%.0fs > %ds, force removing",
                        p.client_order_id, p.symbol, p.action, p.qty_shares, p.limit_price,
                        age, stale_threshold,
                    )
                    store.remove_pending(p.client_order_id)
        except Exception as e:
            logger.warning("[reconcile] stale pending cleanup failed: %s", e.__class__.__name__)

    def _run_once(self):
        """執行一輪所有類股檢查"""
        self._ensure_broker_setup()
        self.last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not self._is_tw_market_open():
            return

        # 先做 reconcile：重啟後 in-flight 訂單兜底
        self._reconcile_brokers()

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
