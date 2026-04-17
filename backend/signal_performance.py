"""
台股信號績效總覽

功能：
1. 對所有追蹤股票（76+檔），從指定日期起逐日回跑五面分析
2. 每支股票產出：逐日五面分數歷史（供 sparkline 趨勢圖）、
   信號觸發日期清單、超選入榜天數、區間漲跌幅等
3. 前端以「全股票總覽大表格」呈現

資料來源：
- 歷史 OHLCV：yfinance
- 籌碼面歷史：FinMind API（三大法人買賣超）
- 技術面引擎：signals.aggregator + 各產業回測最佳權重
- 盤勢層：layers.regime
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from layers.regime import RegimeLayer
from screener import (
    SCREENER_UNIVERSE, get_sector_weights, get_symbol_sector,
)

logger = logging.getLogger(__name__)

# ── 設定 ─────────────────────────────────────────────────────────

ANALYSIS_START = "2026-01-02"
LOOKBACK_DAYS = 200
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PERF_CACHE_FILE = os.path.join(DATA_DIR, "signal_performance_cache.json")
PERF_CACHE_TTL = 3600 * 12  # 12 小時

ETF_SYMBOLS = {"0050.TW", "0056.TW", "00878.TW", "00919.TW"}

REGIME_SCORE_MAP = {
    "強勢多頭": 90, "多頭": 75, "底部轉強": 70,
    "盤整": 50, "高檔轉折": 25, "空頭": 15,
}

# ── FinMind 法人資料 ─────────────────────────────────────────────

INST_CACHE_FILE = os.path.join(DATA_DIR, "backtest", "finmind_inst_cache.json")


def _load_inst_cache() -> Dict:
    try:
        if os.path.exists(INST_CACHE_FILE):
            with open(INST_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _fetch_finmind_institutional(stock_id: str, start: str, end: str) -> Dict[str, dict]:
    """從 FinMind 抓取個股歷史三大法人買賣超"""
    import requests
    url = (
        f"https://api.finmindtrade.com/api/v4/data"
        f"?dataset=TaiwanStockInstitutionalInvestorsBuySell"
        f"&data_id={stock_id}&start_date={start}&end_date={end}"
    )
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return {}
        body = resp.json()
        if body.get("status") != 200 or not body.get("data"):
            return {}
        by_date = {}
        for row in body["data"]:
            dt = row["date"].replace("-", "")
            if dt not in by_date:
                by_date[dt] = {"foreign_net": 0, "trust_net": 0, "dealer_net": 0}
            net = (row.get("buy", 0) or 0) - (row.get("sell", 0) or 0)
            name = row.get("name", "")
            if name == "Foreign_Investor":
                by_date[dt]["foreign_net"] += net
            elif name == "Investment_Trust":
                by_date[dt]["trust_net"] += net
            elif name in ("Dealer_self", "Dealer_Hedging"):
                by_date[dt]["dealer_net"] += net
        return by_date
    except Exception:
        return {}


_inst_cache_loaded = None


def _get_institutional_data(symbol: str) -> Dict[str, dict]:
    """取得法人歷史資料（優先用全域快取）"""
    global _inst_cache_loaded
    code = symbol.replace(".TW", "").replace(".TWO", "")

    if _inst_cache_loaded is None:
        _inst_cache_loaded = _load_inst_cache()

    if code in _inst_cache_loaded and len(_inst_cache_loaded[code]) > 100:
        return _inst_cache_loaded[code]

    data = _fetch_finmind_institutional(code, "2025-10-01", datetime.now().strftime("%Y-%m-%d"))
    return data


def _compute_chip_day(inst_data: Dict[str, dict], date_str: str) -> dict:
    """計算某日的籌碼面信號"""
    if not inst_data:
        return {"foreign_consec_buy": 0, "trust_consec_buy": 0,
                "foreign_net": 0, "trust_net": 0}

    target = date_str.replace("-", "")
    sorted_dates = sorted([d for d in inst_data.keys() if d <= target])
    if not sorted_dates:
        return {"foreign_consec_buy": 0, "trust_consec_buy": 0,
                "foreign_net": 0, "trust_net": 0}

    # 外資連買天數
    foreign_consec = 0
    for d in reversed(sorted_dates):
        if inst_data[d].get("foreign_net", 0) > 0:
            foreign_consec += 1
        else:
            break

    # 投信連買天數
    trust_consec = 0
    for d in reversed(sorted_dates):
        if inst_data[d].get("trust_net", 0) > 0:
            trust_consec += 1
        else:
            break

    today_data = inst_data.get(target, {})
    return {
        "foreign_consec_buy": foreign_consec,
        "trust_consec_buy": trust_consec,
        "foreign_net": today_data.get("foreign_net", 0),
        "trust_net": today_data.get("trust_net", 0),
    }


# ── 股價歷史 ─────────────────────────────────────────────────────

def _fetch_price_history(symbol: str) -> Optional[pd.DataFrame]:
    """取得股價歷史"""
    import yfinance as yf
    start_dt = pd.to_datetime(ANALYSIS_START) - timedelta(days=LOOKBACK_DAYS + 60)
    end_dt = pd.to_datetime(datetime.now().strftime("%Y-%m-%d")) + timedelta(days=1)
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_dt.strftime("%Y-%m-%d"),
                            end=end_dt.strftime("%Y-%m-%d"), interval="1d")
        if df.empty or len(df) < LOOKBACK_DAYS:
            return None
        df.columns = [c.lower() for c in df.columns]
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        return df
    except Exception as e:
        logger.warning(f"取得 {symbol} 歷史失敗: {e}")
        return None


# ── 核心：單一股票完整回跑 ───────────────────────────────────────

def _process_single_stock(symbol: str, name: str) -> Optional[dict]:
    """
    對單一股票回跑所有交易日的五面分析

    Returns:
        {
            symbol, name, sector,
            start_price, end_price, period_return, max_drawdown,
            daily_scores: [{date, close, tech, regime, regime_state, chip, buy_score, sell_score, direction}, ...],
            signals: {
                buy_triggers: [{date, score}, ...],        # buy_score >= 50
                strong_buy_triggers: [{date, score}, ...], # buy_score >= 65
                sell_triggers: [{date, score}, ...],       # sell_score >= 50
                foreign_buy: [{date, days}, ...],          # 外資連買>=3
                trust_buy: [{date, days}, ...],            # 投信連買>=3
                regime_bull: [{date, state}, ...],         # 強勢多頭/多頭
                regime_bottom: [{date, state}, ...],       # 底部轉強
            },
            screener_summary: {
                buy_count, strong_buy_count, sell_count,
                foreign_buy_count, trust_buy_count,
                bull_days, bear_days, consolidation_days
            }
        }
    """
    df = _fetch_price_history(symbol)
    if df is None:
        return None

    inst_data = _get_institutional_data(symbol)
    sector_id = get_symbol_sector(symbol)
    sector_w = get_sector_weights(symbol)

    # 移除 timezone，避免 tz-aware vs tz-naive 比較錯誤
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    start_dt = pd.to_datetime(ANALYSIS_START)
    all_dates = list(df.index)

    # 預算技術指標
    agg = SignalAggregator(weights=sector_w)
    df_full = agg.calculate_all(df.copy())
    regime_layer = RegimeLayer(enabled=True)

    daily_scores = []
    signals = {
        "buy_triggers": [], "strong_buy_triggers": [], "sell_triggers": [],
        "foreign_buy": [], "trust_buy": [],
        "regime_bull": [], "regime_bottom": [],
    }

    # 追蹤用：避免同一信號連日重複記錄
    prev_buy_triggered = False
    prev_strong_buy_triggered = False
    prev_sell_triggered = False
    prev_foreign_buy = False
    prev_trust_buy = False
    prev_regime_bull = False
    prev_regime_bottom = False

    peak = 0
    max_dd = 0

    for i, dt in enumerate(all_dates):
        if dt < start_dt:
            continue
        if i < 120:
            continue

        sub_df = df_full.iloc[:i+1]
        date_str = dt.strftime("%Y-%m-%d")

        # 技術面
        try:
            signal = agg.generate_signals(sub_df, symbol, "1d")
            buy_score = round(signal.buy_score, 1)
            sell_score = round(signal.sell_score, 1)
        except Exception:
            buy_score, sell_score = 0, 0

        # 盤勢層
        try:
            modifier = regime_layer.compute_modifier(symbol, sub_df)
            regime_state = modifier.regime or "未知"
        except Exception:
            regime_state = "未知"
        regime_score = REGIME_SCORE_MAP.get(regime_state, 50)

        # 籌碼面
        chip = _compute_chip_day(inst_data, date_str)
        # 簡易籌碼分數：外資連買+投信連買天數映射
        chip_score = min(100, 50 + chip["foreign_consec_buy"] * 8 + chip["trust_consec_buy"] * 6)

        close = float(sub_df['close'].iloc[-1])

        # 最大回撤
        if close > peak:
            peak = close
        if peak > 0:
            dd = (close / peak - 1) * 100
            if dd < max_dd:
                max_dd = dd

        # 每週取一筆供 sparkline（避免資料過大）
        # 改：每 3 個交易日取一筆 + 最後一筆一定保留
        is_sample = (len(daily_scores) == 0 or
                     (len(daily_scores) > 0 and
                      (pd.to_datetime(date_str) - pd.to_datetime(daily_scores[-1]["date"])).days >= 4))

        if is_sample or dt == all_dates[-1]:
            daily_scores.append({
                "date": date_str,
                "close": round(close, 2),
                "tech": buy_score,
                "regime": regime_score,
                "regime_state": regime_state,
                "chip": chip_score,
            })

        # ── 信號觸發記錄（邊緣觸發：從未觸發→觸發時記錄）──

        # 買入信號
        is_buy = buy_score >= 50
        if is_buy and not prev_buy_triggered:
            signals["buy_triggers"].append({"date": date_str, "score": buy_score})
        prev_buy_triggered = is_buy

        is_strong_buy = buy_score >= 65
        if is_strong_buy and not prev_strong_buy_triggered:
            signals["strong_buy_triggers"].append({"date": date_str, "score": buy_score})
        prev_strong_buy_triggered = is_strong_buy

        # 賣出信號
        is_sell = sell_score >= 50
        if is_sell and not prev_sell_triggered:
            signals["sell_triggers"].append({"date": date_str, "score": sell_score})
        prev_sell_triggered = is_sell

        # 外資連買
        is_foreign = chip["foreign_consec_buy"] >= 3
        if is_foreign and not prev_foreign_buy:
            signals["foreign_buy"].append({"date": date_str, "days": chip["foreign_consec_buy"]})
        prev_foreign_buy = is_foreign

        # 投信連買
        is_trust = chip["trust_consec_buy"] >= 3
        if is_trust and not prev_trust_buy:
            signals["trust_buy"].append({"date": date_str, "days": chip["trust_consec_buy"]})
        prev_trust_buy = is_trust

        # 盤勢多頭
        is_bull = regime_state in ("強勢多頭", "多頭")
        if is_bull and not prev_regime_bull:
            signals["regime_bull"].append({"date": date_str, "state": regime_state})
        prev_regime_bull = is_bull

        # 底部轉強
        is_bottom = regime_state == "底部轉強"
        if is_bottom and not prev_regime_bottom:
            signals["regime_bottom"].append({"date": date_str, "state": regime_state})
        prev_regime_bottom = is_bottom

    if not daily_scores:
        return None

    first_close = daily_scores[0]["close"]
    last_close = daily_scores[-1]["close"]
    period_return = round((last_close / first_close - 1) * 100, 2) if first_close else 0

    # 統計摘要
    screener_summary = {
        "buy_count": len(signals["buy_triggers"]),
        "strong_buy_count": len(signals["strong_buy_triggers"]),
        "sell_count": len(signals["sell_triggers"]),
        "foreign_buy_count": len(signals["foreign_buy"]),
        "trust_buy_count": len(signals["trust_buy"]),
        "regime_bull_count": len(signals["regime_bull"]),
        "regime_bottom_count": len(signals["regime_bottom"]),
    }

    return {
        "symbol": symbol,
        "name": name,
        "sector": sector_id,
        "start_price": first_close,
        "end_price": last_close,
        "period_return": period_return,
        "max_drawdown": round(max_dd, 2),
        "daily_scores": daily_scores,
        "signals": signals,
        "screener_summary": screener_summary,
    }


# ── 主函數 ─────────────────────────────────────────────────────

def run_signal_performance(force_refresh: bool = False) -> dict:
    """執行完整的信號績效計算"""
    if not force_refresh and os.path.exists(PERF_CACHE_FILE):
        try:
            with open(PERF_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cache_time = cached.get("meta", {}).get("generated_at", "")
            if cache_time:
                cache_dt = datetime.strptime(cache_time, "%Y-%m-%d %H:%M")
                if (datetime.now() - cache_dt).total_seconds() < PERF_CACHE_TTL:
                    logger.info("信號績效：使用快取")
                    return cached
        except Exception:
            pass

    logger.info(f"信號績效回測開始：{ANALYSIS_START} ~ 今日")
    start_time = time.time()

    universe = dict(SCREENER_UNIVERSE)  # 包含 ETF

    stocks = []
    completed = 0
    total = len(universe)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for symbol, name in universe.items():
            future = executor.submit(_process_single_stock, symbol, name)
            futures[future] = symbol

        for future in as_completed(futures):
            symbol = futures[future]
            completed += 1
            try:
                result = future.result(timeout=60)
                if result:
                    stocks.append(result)
                if completed % 10 == 0:
                    logger.info(f"  進度: {completed}/{total}")
            except Exception as e:
                logger.warning(f"  {symbol} 處理失敗: {e}")

    # 依漲跌幅排序
    stocks.sort(key=lambda x: x["period_return"], reverse=True)

    elapsed = time.time() - start_time
    logger.info(f"信號回跑完成: {len(stocks)} 檔股票，耗時 {elapsed:.0f}秒")

    result = {
        "stocks": stocks,
        "meta": {
            "start_date": ANALYSIS_START,
            "end_date": datetime.now().strftime("%Y-%m-%d"),
            "total_stocks": len(stocks),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "elapsed_seconds": round(elapsed, 1),
        },
    }

    try:
        os.makedirs(os.path.dirname(PERF_CACHE_FILE), exist_ok=True)
        with open(PERF_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        logger.info(f"信號績效快取已寫入: {PERF_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"寫入快取失敗: {e}")

    return result


def get_performance_results() -> dict:
    """取得績效結果（優先讀快取）"""
    if os.path.exists(PERF_CACHE_FILE):
        try:
            with open(PERF_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"status": "no_cache", "message": "尚未執行績效分析，請先觸發計算"}


_is_running = False


def is_running() -> bool:
    return _is_running


def trigger_background_run():
    """背景執行信號績效計算"""
    global _is_running
    if _is_running:
        return False
    import threading

    def _run():
        global _is_running
        _is_running = True
        try:
            run_signal_performance(force_refresh=True)
        except Exception as e:
            logger.error(f"背景績效計算失敗: {e}")
        finally:
            _is_running = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = run_signal_performance(force_refresh=True)
    print(f"\n完成！{result['meta']['total_stocks']} 檔股票")
    print(f"耗時: {result['meta']['elapsed_seconds']}秒")
