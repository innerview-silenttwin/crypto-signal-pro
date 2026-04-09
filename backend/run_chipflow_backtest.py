"""
籌碼面回測程式
在現有 A/B/C 三組回測基礎上，新增 D 組：技術面 + 盤勢 + 籌碼面

資料來源：
- 技術面 OHLCV：yfinance（同既有回測）
- 盤勢：RegimeLayer（同既有回測）
- 三大法人歷史：FinMind API（TaiwanStockInstitutionalInvestorsBuySell）
- 融資融券：無歷史資料，以中性分(50)帶入

回測設定：
- 期間：2019-01-01 ~ 今日（7年）
- 初始資金：每類股各 100 萬
- 比較：B（技術+盤勢） vs D（技術+盤勢+籌碼）

籌碼面在回測中的影響方式：
- 籌碼分數 >= 70：buy_score * 1.2 + 4（籌碼偏多加分）
- 籌碼分數 >= 55：buy_score * 1.1 + 2
- 籌碼分數 40-55：不調整
- 籌碼分數 < 40：buy_score * 0.8（籌碼偏空扣分）
- 籌碼分數 < 25：veto_buy = True（籌碼嚴重偏空，否決買入）
"""

import sys
import os
import time
import json
import warnings
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import numpy as np
import requests
import yfinance as yf

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator, MarketType
from layers.regime import RegimeLayer
from screener import SECTOR_WEIGHTS, SYMBOL_SECTOR_MAP

# ── 類股股池（同既有回測）──

SECTORS = {
    "半導體": {
        "2330.TW": "台積電", "2454.TW": "聯發科", "2303.TW": "聯電",
        "3711.TW": "日月光投控", "2379.TW": "瑞昱", "3034.TW": "聯詠",
        "6415.TW": "矽力-KY", "2344.TW": "華邦電", "3529.TW": "力旺",
        "5274.TW": "信驊",
    },
    "電子": {
        "2317.TW": "鴻海", "2382.TW": "廣達", "2308.TW": "台達電",
        "2357.TW": "華碩", "3008.TW": "大立光", "2345.TW": "智邦",
        "3231.TW": "緯創", "2356.TW": "英業達", "4938.TW": "和碩",
        "3443.TW": "創意", "2395.TW": "研華", "6669.TW": "緯穎",
        "3037.TW": "欣興", "2327.TW": "國巨", "3661.TW": "世芯-KY",
        "2376.TW": "技嘉", "3017.TW": "奇鋐", "2353.TW": "宏碁",
    },
    "金融": {
        "2881.TW": "富邦金", "2882.TW": "國泰金", "2891.TW": "中信金",
        "2886.TW": "兆豐金", "2884.TW": "玉山金", "2880.TW": "華南金",
        "2887.TW": "台新金", "2890.TW": "永豐金", "2883.TW": "開發金",
        "2892.TW": "第一金", "5880.TW": "合庫金", "2885.TW": "元大金",
    },
    "傳產": {
        "1301.TW": "台塑", "2002.TW": "中鋼", "1216.TW": "統一",
        "2603.TW": "長榮", "2609.TW": "陽明", "2615.TW": "萬海",
        "1303.TW": "南亞", "1326.TW": "台化", "1101.TW": "台泥",
        "2207.TW": "和泰車", "9910.TW": "豐泰",
    },
}

SECTOR_ID_MAP = {
    "半導體": "semiconductor",
    "電子": "electronics",
    "金融": "finance",
    "傳產": "traditional",
}

# ── 回測參數 ──

START_DATE = "2019-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
INITIAL_CAPITAL = 1_000_000
POSITION_PCT = 0.05
MAX_POSITIONS = 20
BUY_THRESHOLD = 50.0
SELL_THRESHOLD = 45.0
STOP_LOSS_PCT = -0.08
TAKE_PROFIT_PCT = 0.20
FEE_BUY = 0.001425
FEE_SELL = 0.001425 + 0.003
MIN_DATA_DAYS = 120

# 籌碼資料快取目錄
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest")
_INST_CACHE_FILE = os.path.join(_DATA_DIR, "finmind_inst_cache.json")


# ── FinMind 歷史法人資料 ──

def _fetch_finmind_bulk(stock_id: str, start_date: str, end_date: str) -> Dict[str, dict]:
    """
    從 FinMind 抓取個股歷史三大法人每日買賣超

    Returns:
        {date_str(YYYYMMDD): {"foreign_net": int, "trust_net": int, "dealer_net": int}}
    """
    url = (
        f"https://api.finmindtrade.com/api/v4/data"
        f"?dataset=TaiwanStockInstitutionalInvestorsBuySell"
        f"&data_id={stock_id}&start_date={start_date}&end_date={end_date}"
    )
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return {}
        body = resp.json()
        if body.get("status") != 200 or not body.get("data"):
            # FinMind 有時 msg 包含限流訊息
            msg = body.get("msg", "")
            if "exceed" in msg.lower() or "limit" in msg.lower():
                print(f"    ⚠️ FinMind 限流: {msg}")
            return {}

        by_date: Dict[str, dict] = {}
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
    except Exception as e:
        print(f"    ⚠️ FinMind 抓取失敗 ({stock_id}): {e}")
        return {}


def load_inst_cache() -> Dict:
    """讀取本地法人資料快取"""
    if os.path.exists(_INST_CACHE_FILE):
        try:
            with open(_INST_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_inst_cache(cache: Dict):
    """儲存法人資料快取"""
    os.makedirs(os.path.dirname(_INST_CACHE_FILE), exist_ok=True)
    with open(_INST_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def fetch_all_institutional_data(sectors: Dict) -> Dict[str, Dict[str, dict]]:
    """
    批次下載所有股票的歷史法人資料

    Returns:
        {symbol: {date_str: {"foreign_net": int, "trust_net": int, "dealer_net": int}}}
    """
    cache = load_inst_cache()
    all_symbols = []
    for symbols_dict in sectors.values():
        all_symbols.extend(symbols_dict.keys())

    result = {}
    need_fetch = []

    for sym in all_symbols:
        code = sym.replace(".TW", "").replace(".TWO", "")
        if code in cache and len(cache[code]) > 500:
            # 已有充足快取資料（至少 500 個交易日）
            result[sym] = cache[code]
        else:
            need_fetch.append(sym)

    if need_fetch:
        print(f"\n  需從 FinMind 下載 {len(need_fetch)} 檔法人歷史資料...")
        print(f"  （已快取 {len(result)} 檔）")

        for i, sym in enumerate(need_fetch):
            code = sym.replace(".TW", "").replace(".TWO", "")
            print(f"    [{i+1}/{len(need_fetch)}] 下載 {code}...", end="", flush=True)

            data = _fetch_finmind_bulk(code, START_DATE, END_DATE)
            if data:
                result[sym] = data
                cache[code] = data
                print(f" ✓ {len(data)} 天")
            else:
                result[sym] = {}
                print(f" ✗ 無資料")

            # FinMind 免費版限流：每次間隔 1 秒
            if i < len(need_fetch) - 1:
                time.sleep(1.0)

        # 儲存快取
        save_inst_cache(cache)
        print(f"  法人資料快取已儲存 ({len(cache)} 檔)")
    else:
        print(f"\n  法人資料全部從快取載入（{len(result)} 檔）")

    return result


# ── 籌碼分數計算（回測用，簡化版）──

def compute_backtest_chip_score(
    inst_data: Dict[str, dict],
    current_date: str,
    lookback_days: int = 5,
) -> Tuple[float, bool]:
    """
    根據歷史法人資料計算籌碼分數（回測用）

    只用三大法人資料（佔原始籌碼分數的 65%），
    融資融券無歷史資料，以中性分帶入。

    Args:
        inst_data: {date_str: {"foreign_net", "trust_net", "dealer_net"}}
        current_date: 目前日期 YYYYMMDD
        lookback_days: 回看天數

    Returns:
        (chip_score 0-100, veto_buy)
    """
    if not inst_data:
        return 50.0, False  # 無資料=中性

    # 取最近 N 天有資料的日期
    sorted_dates = sorted(
        [d for d in inst_data.keys() if d <= current_date],
        reverse=True
    )[:lookback_days]

    if not sorted_dates:
        return 50.0, False

    recent_data = [inst_data[d] for d in sorted_dates]

    # ── 1. 外資分數 (30%) ──
    def _consec_days(data_list, key):
        if not data_list:
            return 0
        first_val = data_list[0].get(key, 0)
        if first_val == 0:
            return 0
        direction = 1 if first_val > 0 else -1
        count = 0
        for d in data_list:
            val = d.get(key, 0)
            if (direction > 0 and val > 0) or (direction < 0 and val < 0):
                count += 1
            else:
                break
        return count * direction

    fc = _consec_days(recent_data, "foreign_net")
    if fc >= 5:
        foreign_score = 90
    elif fc >= 3:
        foreign_score = 75
    elif fc >= 1:
        foreign_score = 60
    elif fc == 0:
        foreign_score = 50
    elif fc >= -2:
        foreign_score = 35
    elif fc >= -4:
        foreign_score = 25
    else:
        foreign_score = 15

    # 累計金額加成
    ft = sum(d.get("foreign_net", 0) for d in recent_data)
    if ft > 50000:
        foreign_score = min(100, foreign_score + 10)
    elif ft < -50000:
        foreign_score = max(0, foreign_score - 10)

    # ── 2. 投信分數 (25%) ──
    tc = _consec_days(recent_data, "trust_net")
    if tc >= 5:
        trust_score = 92
    elif tc >= 3:
        trust_score = 85
    elif tc >= 1:
        trust_score = 65
    elif tc == 0:
        trust_score = 50
    elif tc >= -2:
        trust_score = 30
    else:
        trust_score = 20

    # ── 3. 自營商分數 (10%) ──
    dt = sum(d.get("dealer_net", 0) for d in recent_data)
    if dt > 10000:
        dealer_score = 70
    elif dt > 0:
        dealer_score = 60
    elif dt == 0:
        dealer_score = 50
    elif dt > -10000:
        dealer_score = 40
    else:
        dealer_score = 30

    # ── 4 & 5. 融資融券（無歷史資料，以中性帶入）──
    margin_score = 50
    short_score = 50

    # ── 加權計算總分 ──
    total_score = (
        foreign_score * 0.30 +
        trust_score * 0.25 +
        dealer_score * 0.10 +
        margin_score * 0.20 +
        short_score * 0.15
    )
    total_score = max(0, min(100, round(total_score)))

    # 特殊加成：外資+投信同步連買
    if fc >= 3 and tc >= 3:
        total_score = min(100, total_score + 5)

    veto_buy = total_score < 25
    return float(total_score), veto_buy


# ── 信號計算（含籌碼面）──

def compute_score_with_chip(
    df_window: pd.DataFrame,
    symbol: str,
    aggregator: SignalAggregator,
    regime_layer: Optional[RegimeLayer],
    inst_data: Optional[Dict[str, dict]],
    current_date_str: str,
    sector_id: str = "",
) -> Tuple[float, float, bool, List[str]]:
    """
    計算買入/賣出分數（含籌碼面修正）

    Returns:
        (buy_score, sell_score, veto_buy, buy_indicator_names)
    """
    if len(df_window) < MIN_DATA_DAYS:
        return 0.0, 0.0, False, []

    try:
        df_calc = aggregator.calculate_all(df_window.copy())
        signal = aggregator.generate_signals(df_calc, symbol=symbol, timeframe="1d")

        buy_score = signal.buy_score
        sell_score = signal.sell_score
        veto_buy = False
        buy_indicator_names: List[str] = [s.indicator_name for s in signal.buy_signals]

        # Regime 修正
        if regime_layer is not None:
            modifier = regime_layer.compute_modifier(symbol, df_calc)
            if modifier.active:
                # 傳產 Regime Veto-Only
                if sector_id == "traditional" and modifier.regime in ("強勢多頭", "多頭"):
                    # 只否決，不加乘
                    pass
                else:
                    if modifier.veto_buy:
                        veto_buy = True
                    buy_score = buy_score * modifier.buy_multiplier + modifier.buy_offset
                    sell_score = sell_score * modifier.sell_multiplier + modifier.sell_offset

        # 籌碼面修正
        if inst_data is not None:
            chip_score, chip_veto = compute_backtest_chip_score(
                inst_data, current_date_str
            )

            if chip_score >= 70:
                buy_score = buy_score * 1.20 + 4.0
                sell_score = sell_score * 0.80
            elif chip_score >= 55:
                buy_score = buy_score * 1.10 + 2.0
                sell_score = sell_score * 0.90
            elif chip_score < 40:
                buy_score = buy_score * 0.80
                sell_score = sell_score * 1.10
            # chip_score 40-55: 中性，不調整

            if chip_veto:
                veto_buy = True

        return float(buy_score), float(sell_score), veto_buy, buy_indicator_names

    except Exception:
        return 0.0, 0.0, False, []


# ── 資料下載 ──

def fetch_tw_data(symbols: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
    """批次下載 yfinance 台股日線資料"""
    print(f"  下載 {len(symbols)} 檔 OHLCV 資料中...")
    result = {}
    raw = yf.download(
        symbols, start=start, end=end,
        auto_adjust=True, progress=False, threads=True
    )

    if isinstance(raw.columns, pd.MultiIndex):
        for sym in symbols:
            try:
                df = raw.xs(sym, axis=1, level=1).copy()
                df.columns = [c.lower() for c in df.columns]
                df = df.dropna(subset=["close"])
                if len(df) >= MIN_DATA_DAYS:
                    result[sym] = df
            except Exception:
                pass
    else:
        if len(symbols) == 1:
            df = raw.copy()
            df.columns = [c.lower() for c in df.columns]
            df = df.dropna(subset=["close"])
            if len(df) >= MIN_DATA_DAYS:
                result[symbols[0]] = df

    print(f"  成功載入 {len(result)}/{len(symbols)} 檔")
    return result


# ── 投組回測引擎 ──

@dataclass
class Position:
    symbol: str
    entry_price: float
    entry_date: pd.Timestamp
    shares: float
    cost: float
    entry_score: float = 0.0
    entry_indicators: str = ""


@dataclass
class SectorResult:
    sector: str
    mode: str
    initial_capital: float
    final_capital: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_trades: int
    win_trades: int
    win_rate: float
    avg_hold_days: float
    profit_factor: float
    equity_curve: List[float] = field(default_factory=list)
    trade_log: List[dict] = field(default_factory=list)


def run_portfolio_backtest(
    sector_name: str,
    stock_data: Dict[str, pd.DataFrame],
    use_regime: bool,
    use_chipflow: bool,
    inst_data_all: Optional[Dict[str, Dict[str, dict]]] = None,
    buy_threshold: float = BUY_THRESHOLD,
    sell_threshold: float = SELL_THRESHOLD,
    stop_loss_pct: float = STOP_LOSS_PCT,
    take_profit_pct: float = TAKE_PROFIT_PCT,
    mode_label: str = "",
    sector_id: str = "",
) -> SectorResult:
    """逐日模擬投組交易"""
    mode = mode_label or ("chipflow" if use_chipflow else ("regime" if use_regime else "baseline"))
    regime_str = "+盤勢" if use_regime else ""
    chip_str = "+籌碼" if use_chipflow else ""
    print(f"\n  [{sector_name}] 執行 {mode}（技術{regime_str}{chip_str}）...")

    # 使用產業專屬技術指標權重
    weights = SECTOR_WEIGHTS.get(sector_id, SECTOR_WEIGHTS.get("default"))
    aggregator = SignalAggregator(market_type=MarketType.STOCK, weights=weights)
    regime_layer = RegimeLayer() if use_regime else None

    all_dates = sorted(set(
        date for df in stock_data.values() for date in df.index
    ))
    all_dates = [d for d in all_dates
                 if d >= pd.Timestamp(START_DATE) and d <= pd.Timestamp(END_DATE)]

    capital = float(INITIAL_CAPITAL)
    positions: Dict[str, Position] = {}
    equity_curve = [capital]
    trade_log = []
    peak = capital

    LOOKBACK = 200

    for date_idx, date in enumerate(all_dates):
        date_str = date.strftime("%Y%m%d")

        # ── 1. 檢查現有持倉 ──
        symbols_to_close = []
        for sym, pos in positions.items():
            if sym not in stock_data or date not in stock_data[sym].index:
                continue
            current_price = stock_data[sym].loc[date, "close"]
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price

            if pnl_pct <= stop_loss_pct:
                symbols_to_close.append((sym, current_price, "停損"))
                continue
            if pnl_pct >= take_profit_pct:
                symbols_to_close.append((sym, current_price, "停利"))
                continue

            df_sym = stock_data[sym]
            loc = df_sym.index.get_loc(date)
            if loc >= MIN_DATA_DAYS:
                window = df_sym.iloc[max(0, loc - LOOKBACK): loc + 1]
                sym_inst = inst_data_all.get(sym, {}) if use_chipflow and inst_data_all else None
                buy_s, sell_s, _, _inds = compute_score_with_chip(
                    window, sym, aggregator, regime_layer,
                    sym_inst, date_str, sector_id
                )
                if sell_s > buy_s and sell_s >= sell_threshold:
                    symbols_to_close.append((sym, current_price, "賣出信號"))

        for sym, price, reason in symbols_to_close:
            pos = positions.pop(sym)
            proceeds = pos.shares * price * (1 - FEE_SELL)
            pnl = proceeds - pos.cost
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
            hold_days = (date - pos.entry_date).days
            capital += proceeds
            trade_log.append({
                "symbol": sym, "entry_date": pos.entry_date, "exit_date": date,
                "entry_price": pos.entry_price, "exit_price": price,
                "pnl_pct": pnl_pct, "pnl": pnl, "hold_days": hold_days,
                "exit_reason": reason,
                "entry_score": pos.entry_score,
                "entry_indicators": pos.entry_indicators,
            })

        # ── 2. 掃描買入信號 ──
        available_slots = MAX_POSITIONS - len(positions)
        if available_slots > 0 and capital > INITIAL_CAPITAL * POSITION_PCT:
            candidates = []
            for sym, df_sym in stock_data.items():
                if sym in positions:
                    continue
                if date not in df_sym.index:
                    continue
                loc = df_sym.index.get_loc(date)
                if loc < MIN_DATA_DAYS:
                    continue
                window = df_sym.iloc[max(0, loc - LOOKBACK): loc + 1]
                sym_inst = inst_data_all.get(sym, {}) if use_chipflow and inst_data_all else None
                buy_s, sell_s, veto, entry_inds = compute_score_with_chip(
                    window, sym, aggregator, regime_layer,
                    sym_inst, date_str, sector_id
                )
                if not veto and buy_s >= buy_threshold and buy_s > sell_s:
                    candidates.append((sym, buy_s, entry_inds))

            candidates.sort(key=lambda x: x[1], reverse=True)
            for sym, score, inds in candidates[:available_slots]:
                if capital < INITIAL_CAPITAL * POSITION_PCT:
                    break
                price = stock_data[sym].loc[date, "close"]
                invest = capital * POSITION_PCT
                cost_with_fee = invest * (1 + FEE_BUY)
                if cost_with_fee > capital:
                    continue
                shares = invest / price
                capital -= cost_with_fee
                positions[sym] = Position(
                    symbol=sym, entry_price=price, entry_date=date,
                    shares=shares, cost=cost_with_fee,
                    entry_score=score, entry_indicators=",".join(inds),
                )

        # ── 3. 計算當日總資產 ──
        holdings_value = sum(
            pos.shares * stock_data[sym].loc[date, "close"]
            for sym, pos in positions.items()
            if sym in stock_data and date in stock_data[sym].index
        )
        total_equity = capital + holdings_value
        equity_curve.append(total_equity)
        peak = max(peak, total_equity)

    # ── 強制平倉 ──
    last_date = all_dates[-1]
    for sym, pos in list(positions.items()):
        if sym in stock_data and last_date in stock_data[sym].index:
            price = stock_data[sym].loc[last_date, "close"]
            proceeds = pos.shares * price * (1 - FEE_SELL)
            pnl = proceeds - pos.cost
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
            capital += proceeds
            trade_log.append({
                "symbol": sym, "entry_date": pos.entry_date, "exit_date": last_date,
                "entry_price": pos.entry_price, "exit_price": price,
                "pnl_pct": pnl_pct, "pnl": pnl,
                "hold_days": (last_date - pos.entry_date).days,
                "exit_reason": "期末強制平倉",
                "entry_score": pos.entry_score,
                "entry_indicators": pos.entry_indicators,
            })

    # ── 績效計算 ──
    final_capital = capital
    total_return = (final_capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    eq = pd.Series(equity_curve)
    running_max = eq.cummax()
    drawdowns = (eq - running_max) / running_max * 100
    max_dd = drawdowns.min()

    closed = trade_log
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    avg_hold = sum(t["hold_days"] for t in closed) / len(closed) if closed else 0

    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    eq_returns = eq.pct_change().dropna()
    if eq_returns.std() > 0:
        sharpe = (eq_returns.mean() / eq_returns.std()) * np.sqrt(252)
    else:
        sharpe = 0.0

    return SectorResult(
        sector=sector_name, mode=mode,
        initial_capital=INITIAL_CAPITAL, final_capital=final_capital,
        total_return_pct=total_return, max_drawdown_pct=float(max_dd),
        sharpe_ratio=float(sharpe), total_trades=len(closed),
        win_trades=len(wins), win_rate=win_rate,
        avg_hold_days=avg_hold, profit_factor=profit_factor,
        equity_curve=equity_curve, trade_log=trade_log,
    )


# ── 報告輸出 ──

def print_comparison(results: Dict[str, Dict[str, SectorResult]]):
    """印出比較表"""
    print("\n" + "=" * 95)
    print(f"  籌碼面回測報告  |  {START_DATE} ~ {END_DATE}  |  初始資金: 100 萬/類股")
    print(f"  B=技術+盤勢  D=技術+盤勢+籌碼")
    print("=" * 95)

    header = (
        f"{'類股':<6} {'模式':<18} {'總報酬':>9} {'年化':>7} {'最大回撤':>9} "
        f"{'夏普':>6} {'交易數':>6} {'勝率':>7} {'獲利因子':>8} {'均持天':>7}"
    )
    print(header)
    print("-" * 95)

    years = (pd.Timestamp(END_DATE) - pd.Timestamp(START_DATE)).days / 365.25

    for sector_name, mode_results in results.items():
        for mode, r in mode_results.items():
            ann_return = ((1 + r.total_return_pct / 100) ** (1 / years) - 1) * 100 if years > 0 else 0
            pf_str = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
            print(
                f"{sector_name:<6} "
                f"{mode:<18} "
                f"{r.total_return_pct:>+8.1f}% "
                f"{ann_return:>+6.1f}% "
                f"{r.max_drawdown_pct:>8.1f}% "
                f"{r.sharpe_ratio:>6.2f} "
                f"{r.total_trades:>6} "
                f"{r.win_rate:>6.1f}% "
                f"{pf_str:>8} "
                f"{r.avg_hold_days:>6.0f}天"
            )
        print("-" * 95)

    # ── B vs D 對比 ──
    print("\n【籌碼面增益分析（B → D）】")
    print(f"{'類股':<6} {'B報酬':>9} {'D報酬':>9} {'報酬改善':>10} {'B夏普':>7} {'D夏普':>7} {'夏普改善':>9} {'D勝率':>8}")
    print("-" * 75)
    for sector_name, mode_results in results.items():
        b = mode_results.get("B_技術+盤勢")
        d = mode_results.get("D_技術+盤勢+籌碼")
        if b and d:
            print(
                f"{sector_name:<6} "
                f"{b.total_return_pct:>+8.1f}% "
                f"{d.total_return_pct:>+8.1f}% "
                f"{d.total_return_pct - b.total_return_pct:>+9.1f}%  "
                f"{b.sharpe_ratio:>6.2f} "
                f"{d.sharpe_ratio:>6.2f} "
                f"{d.sharpe_ratio - b.sharpe_ratio:>+8.2f}  "
                f"{d.win_rate:>7.1f}%"
            )
    print("=" * 95)


def save_results(results: Dict[str, Dict[str, SectorResult]]):
    """儲存回測結果"""
    os.makedirs(_DATA_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    all_trades = []
    for sector, mode_results in results.items():
        for mode, r in mode_results.items():
            for t in r.trade_log:
                all_trades.append({"sector": sector, "mode": mode, **t})

    if all_trades:
        df_trades = pd.DataFrame(all_trades)
        path = os.path.join(_DATA_DIR, f"chipflow_backtest_{timestamp}.csv")
        df_trades.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n  交易明細已儲存：{path}")

    # 儲存摘要報告
    summary_path = os.path.join(_DATA_DIR, f"chipflow_backtest_report_{timestamp}.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"籌碼面回測報告  |  {START_DATE} ~ {END_DATE}\n")
        f.write(f"B=技術+盤勢  D=技術+盤勢+籌碼\n\n")
        years = (pd.Timestamp(END_DATE) - pd.Timestamp(START_DATE)).days / 365.25
        for sector_name, mode_results in results.items():
            f.write(f"\n【{sector_name}】\n")
            for mode, r in mode_results.items():
                ann = ((1 + r.total_return_pct / 100) ** (1 / years) - 1) * 100 if years > 0 else 0
                pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
                f.write(
                    f"  {mode}: 報酬{r.total_return_pct:+.1f}%  年化{ann:+.1f}%  "
                    f"回撤{r.max_drawdown_pct:.1f}%  夏普{r.sharpe_ratio:.2f}  "
                    f"交易{r.total_trades}筆  勝率{r.win_rate:.1f}%  獲利因子{pf}\n"
                )
        # 增益分析
        f.write("\n\n【籌碼面增益分析】\n")
        for sector_name, mode_results in results.items():
            b = mode_results.get("B_技術+盤勢")
            d = mode_results.get("D_技術+盤勢+籌碼")
            if b and d:
                f.write(
                    f"  {sector_name}: 報酬 {b.total_return_pct:+.1f}% → {d.total_return_pct:+.1f}% "
                    f"({d.total_return_pct - b.total_return_pct:+.1f}%)  "
                    f"夏普 {b.sharpe_ratio:.2f} → {d.sharpe_ratio:.2f} "
                    f"({d.sharpe_ratio - b.sharpe_ratio:+.2f})\n"
                )
    print(f"  摘要報告已儲存：{summary_path}")


# ── 主程式 ──

def main():
    print("=" * 80)
    print(f"  籌碼面回測  |  {START_DATE} ~ {END_DATE}")
    print(f"  對比：B（技術+盤勢） vs D（技術+盤勢+籌碼）")
    print(f"  籌碼來源：FinMind 三大法人歷史（融資融券以中性帶入）")
    print("=" * 80)

    # 1. 下載所有法人歷史資料
    print("\n📊 Phase 1: 下載法人歷史資料")
    inst_data_all = fetch_all_institutional_data(SECTORS)

    all_results: Dict[str, Dict[str, SectorResult]] = {}

    for sector_name, symbols_dict in SECTORS.items():
        print(f"\n{'─'*60}")
        print(f"  類股：{sector_name}（{len(symbols_dict)} 檔）")
        print(f"{'─'*60}")

        sector_id = SECTOR_ID_MAP.get(sector_name, "default")
        symbols = list(symbols_dict.keys())

        # 2. 下載 OHLCV
        stock_data = fetch_tw_data(symbols, START_DATE, END_DATE)
        if not stock_data:
            print(f"  ⚠️ {sector_name} 無可用資料，跳過")
            continue

        # 篩選有法人資料的股票
        chip_coverage = sum(1 for s in stock_data if inst_data_all.get(s))
        print(f"  法人資料覆蓋：{chip_coverage}/{len(stock_data)} 檔")

        sector_results = {}

        # B：技術+盤勢（基準）
        t0 = time.time()
        r_regime = run_portfolio_backtest(
            sector_name, stock_data,
            use_regime=True, use_chipflow=False,
            mode_label="B_技術+盤勢", sector_id=sector_id,
        )
        sector_results["B_技術+盤勢"] = r_regime
        print(f"    B 完成（{time.time()-t0:.0f}s）"
              f"報酬: {r_regime.total_return_pct:+.1f}%  "
              f"夏普: {r_regime.sharpe_ratio:.2f}  "
              f"勝率: {r_regime.win_rate:.1f}%")

        # D：技術+盤勢+籌碼
        t0 = time.time()
        r_chip = run_portfolio_backtest(
            sector_name, stock_data,
            use_regime=True, use_chipflow=True,
            inst_data_all=inst_data_all,
            mode_label="D_技術+盤勢+籌碼", sector_id=sector_id,
        )
        sector_results["D_技術+盤勢+籌碼"] = r_chip
        print(f"    D 完成（{time.time()-t0:.0f}s）"
              f"報酬: {r_chip.total_return_pct:+.1f}%  "
              f"夏普: {r_chip.sharpe_ratio:.2f}  "
              f"勝率: {r_chip.win_rate:.1f}%")

        # 增益
        delta_ret = r_chip.total_return_pct - r_regime.total_return_pct
        delta_sharpe = r_chip.sharpe_ratio - r_regime.sharpe_ratio
        print(f"    → 籌碼增益：報酬 {delta_ret:+.1f}%  夏普 {delta_sharpe:+.2f}")

        all_results[sector_name] = sector_results

    print_comparison(all_results)
    save_results(all_results)


if __name__ == "__main__":
    main()
