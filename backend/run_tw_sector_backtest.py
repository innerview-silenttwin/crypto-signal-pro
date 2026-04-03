"""
台股類股投組回測主程式
比較：純技術信號 vs. 加入 Regime Layer 的差異

設定：
- 資料來源：yfinance（7 年日線，2019-01-01 起）
- 初始資金：每個類股各 100 萬
- 部位大小：每筆 5%（最多同時持有 20 檔）
- 停損：-8%，停利：+20%
- 手續費：買0.1425% + 賣0.1425% + 證交稅0.3%
- 比較模式：baseline（純技術）vs. regime（技術 + 盤勢層）
"""

import sys
import os
import time
import warnings
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import pandas as pd
import numpy as np
import yfinance as yf

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator, MarketType
from layers.regime import RegimeLayer

# ── 類股股池 ──────────────────────────────────────────────────────

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

# ── 回測參數 ──────────────────────────────────────────────────────

START_DATE = "2019-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
INITIAL_CAPITAL = 1_000_000      # 100 萬
POSITION_PCT = 0.05              # 每筆 5%
MAX_POSITIONS = 20               # 最多 20 檔
BUY_THRESHOLD = 50.0             # 買入門檻分
SELL_THRESHOLD = 45.0            # 賣出門檻分
STOP_LOSS_PCT = -0.08            # 停損 -8%
TAKE_PROFIT_PCT = 0.20           # 停利 +20%
FEE_BUY = 0.001425               # 買進手續費
FEE_SELL = 0.001425 + 0.003      # 賣出手續費 + 證交稅
MIN_DATA_DAYS = 120              # 技術指標最少需要的資料天數


# ── 資料下載 ──────────────────────────────────────────────────────

def fetch_tw_data(symbols: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
    """批次下載 yfinance 台股日線資料"""
    print(f"  下載 {len(symbols)} 檔資料中...")
    result = {}
    # 批次下載，比一次一檔快
    raw = yf.download(
        symbols, start=start, end=end,
        auto_adjust=True, progress=False, threads=True
    )

    if isinstance(raw.columns, pd.MultiIndex):
        # 多檔格式：columns = (field, symbol)
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
        # 單檔格式
        if len(symbols) == 1:
            df = raw.copy()
            df.columns = [c.lower() for c in df.columns]
            df = df.dropna(subset=["close"])
            if len(df) >= MIN_DATA_DAYS:
                result[symbols[0]] = df

    print(f"  成功載入 {len(result)}/{len(symbols)} 檔")
    return result


# ── 信號計算 ──────────────────────────────────────────────────────

def compute_score(df_window: pd.DataFrame, symbol: str,
                  aggregator: SignalAggregator,
                  regime_layer: Optional[RegimeLayer]) -> Tuple[float, float, bool]:
    """
    計算指定窗口的買入/賣出分數

    Returns:
        (buy_score, sell_score, veto_buy)
    """
    if len(df_window) < MIN_DATA_DAYS:
        return 0.0, 0.0, False

    try:
        df_calc = aggregator.calculate_all(df_window.copy())
        signal = aggregator.generate_signals(df_calc, symbol=symbol, timeframe="1d")

        buy_score = signal.buy_score
        sell_score = signal.sell_score
        veto_buy = False

        if regime_layer is not None:
            modifier = regime_layer.compute_modifier(symbol, df_calc)
            if modifier.active:
                if modifier.veto_buy:
                    veto_buy = True
                buy_score = buy_score * modifier.buy_multiplier + modifier.buy_offset
                sell_score = sell_score * modifier.sell_multiplier + modifier.sell_offset

        return float(buy_score), float(sell_score), veto_buy

    except Exception:
        return 0.0, 0.0, False


# ── 投組回測引擎 ──────────────────────────────────────────────────

@dataclass
class Position:
    symbol: str
    entry_price: float
    entry_date: pd.Timestamp
    shares: float       # 持有股數（含小數，以金額計）
    cost: float         # 買進成本（含手續費）


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
    buy_threshold: float = BUY_THRESHOLD,
    sell_threshold: float = SELL_THRESHOLD,
    stop_loss_pct: float = STOP_LOSS_PCT,
    take_profit_pct: float = TAKE_PROFIT_PCT,
    mode_label: str = "",
) -> SectorResult:
    """
    逐日模擬投組交易

    每個交易日：
    1. 先檢查現有持倉的停損/停利/賣出信號
    2. 掃描全部股票的買入信號
    3. 按分數排序，補位到最多 MAX_POSITIONS 檔
    """
    mode = mode_label if mode_label else ("regime" if use_regime else "baseline")
    print(f"\n  [{sector_name}] 執行 {mode} 回測（門檻:{buy_threshold} 停利:{take_profit_pct*100:.0f}%）...")

    aggregator = SignalAggregator(market_type=MarketType.STOCK)
    regime_layer = RegimeLayer() if use_regime else None

    # 取所有股票的交易日聯集，排序
    all_dates = sorted(set(
        date
        for df in stock_data.values()
        for date in df.index
    ))
    all_dates = [d for d in all_dates
                 if d >= pd.Timestamp(START_DATE) and d <= pd.Timestamp(END_DATE)]

    capital = float(INITIAL_CAPITAL)
    positions: Dict[str, Position] = {}
    equity_curve = [capital]
    trade_log = []
    peak = capital

    LOOKBACK = 200  # 每次計算用最近 N 天

    for date_idx, date in enumerate(all_dates):
        # ── 1. 檢查現有持倉 ──
        symbols_to_close = []
        for sym, pos in positions.items():
            if sym not in stock_data or date not in stock_data[sym].index:
                continue
            current_price = stock_data[sym].loc[date, "close"]
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price

            # 停損 / 停利
            if pnl_pct <= stop_loss_pct:
                symbols_to_close.append((sym, current_price, "停損"))
                continue
            if pnl_pct >= take_profit_pct:
                symbols_to_close.append((sym, current_price, "停利"))
                continue

            # 賣出信號
            df_sym = stock_data[sym]
            loc = df_sym.index.get_loc(date)
            if loc >= MIN_DATA_DAYS:
                window = df_sym.iloc[max(0, loc - LOOKBACK): loc + 1]
                buy_s, sell_s, _ = compute_score(window, sym, aggregator, regime_layer)
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
                buy_s, sell_s, veto = compute_score(window, sym, aggregator, regime_layer)
                if not veto and buy_s >= buy_threshold and buy_s > sell_s:
                    candidates.append((sym, buy_s))

            # 按分數排序，選前 N 個
            candidates.sort(key=lambda x: x[1], reverse=True)
            for sym, score in candidates[:available_slots]:
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

    # ── 強制平倉剩餘部位（以最後一天收盤價） ──
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
            })

    # ── 計算績效指標 ──
    final_capital = capital
    total_return = (final_capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    # 最大回撤
    eq = pd.Series(equity_curve)
    running_max = eq.cummax()
    drawdowns = (eq - running_max) / running_max * 100
    max_dd = drawdowns.min()

    # 勝率
    closed = trade_log
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    avg_hold = sum(t["hold_days"] for t in closed) / len(closed) if closed else 0

    # 獲利因子
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # 夏普比率（年化，日報酬）
    eq_returns = eq.pct_change().dropna()
    if eq_returns.std() > 0:
        sharpe = (eq_returns.mean() / eq_returns.std()) * np.sqrt(252)
    else:
        sharpe = 0.0

    return SectorResult(
        sector=sector_name,
        mode=mode,
        initial_capital=INITIAL_CAPITAL,
        final_capital=final_capital,
        total_return_pct=total_return,
        max_drawdown_pct=float(max_dd),
        sharpe_ratio=float(sharpe),
        total_trades=len(closed),
        win_trades=len(wins),
        win_rate=win_rate,
        avg_hold_days=avg_hold,
        profit_factor=profit_factor,
        equity_curve=equity_curve,
        trade_log=trade_log,
    )


# ── 報告輸出 ──────────────────────────────────────────────────────

def print_comparison(results: Dict[str, Dict[str, SectorResult]]):
    """印出四類股、三模式的比較表"""
    print("\n" + "=" * 90)
    print(f"  台股類股回測報告  |  {START_DATE} ~ {END_DATE}  |  初始資金: 100 萬/類股")
    print(f"  A=純技術(門檻50,停利20%)  B=技術+盤勢(門檻50,停利20%)  C=最佳化(門檻60,停利25%,盤勢)")
    print("=" * 90)

    header = f"{'類股':<6} {'模式':<12} {'總報酬':>9} {'年化':>7} {'最大回撤':>9} {'夏普':>6} {'交易數':>6} {'勝率':>7} {'獲利因子':>8} {'均持天':>7}"
    print(header)
    print("-" * 90)

    years = (pd.Timestamp(END_DATE) - pd.Timestamp(START_DATE)).days / 365.25

    for sector_name, mode_results in results.items():
        for mode, r in mode_results.items():
            ann_return = ((1 + r.total_return_pct / 100) ** (1 / years) - 1) * 100 if years > 0 else 0
            pf_str = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞"
            print(
                f"{sector_name:<6} "
                f"{mode:<12} "
                f"{r.total_return_pct:>+8.1f}% "
                f"{ann_return:>+6.1f}% "
                f"{r.max_drawdown_pct:>8.1f}% "
                f"{r.sharpe_ratio:>6.2f} "
                f"{r.total_trades:>6} "
                f"{r.win_rate:>6.1f}% "
                f"{pf_str:>8} "
                f"{r.avg_hold_days:>6.0f}天"
            )
        print("-" * 90)

    # 彙總：A vs B vs C
    print("\n【三組策略對比（以 A 純技術為基準）】")
    print(f"{'類股':<6} {'B報酬改善':>10} {'B夏普改善':>10} {'C報酬改善':>10} {'C夏普改善':>10} {'C勝率':>8}")
    print("-" * 60)
    for sector_name, mode_results in results.items():
        a = mode_results.get("A_純技術")
        b = mode_results.get("B_技術+盤勢")
        c = mode_results.get("C_最佳化")
        if a and b and c:
            print(
                f"{sector_name:<6} "
                f"{b.total_return_pct - a.total_return_pct:>+9.1f}%  "
                f"{b.sharpe_ratio - a.sharpe_ratio:>+9.2f}  "
                f"{c.total_return_pct - a.total_return_pct:>+9.1f}%  "
                f"{c.sharpe_ratio - a.sharpe_ratio:>+9.2f}  "
                f"{c.win_rate:>7.1f}%"
            )
    print("=" * 90)


def save_results(results: Dict[str, Dict[str, SectorResult]]):
    """儲存詳細交易記錄到 CSV"""
    import csv
    os.makedirs("backend/data/backtest", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    all_trades = []
    for sector, mode_results in results.items():
        for mode, r in mode_results.items():
            for t in r.trade_log:
                all_trades.append({
                    "sector": sector,
                    "mode": mode,
                    **t,
                })

    if all_trades:
        df_trades = pd.DataFrame(all_trades)
        path = f"backend/data/backtest/tw_backtest_{timestamp}.csv"
        df_trades.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n  交易明細已儲存：{path}")

    # 儲存資金曲線
    equity_data = {}
    for sector, mode_results in results.items():
        for mode, r in mode_results.items():
            key = f"{sector}_{mode}"
            equity_data[key] = r.equity_curve

    # 對齊長度
    max_len = max(len(v) for v in equity_data.values())
    for k in equity_data:
        last = equity_data[k][-1]
        equity_data[k] = equity_data[k] + [last] * (max_len - len(equity_data[k]))

    df_eq = pd.DataFrame(equity_data)
    eq_path = f"backend/data/backtest/equity_curve_{timestamp}.csv"
    df_eq.to_csv(eq_path, index=False, encoding="utf-8-sig")
    print(f"  資金曲線已儲存：{eq_path}")


# ── 主程式 ────────────────────────────────────────────────────────

def main():
    print("=" * 80)
    print(f"  台股類股回測  |  {START_DATE} ~ {END_DATE}  |  7 年")
    print(f"  三組對比：純技術 / 技術+盤勢 / 最佳化（門檻60+停利25%+盤勢）")
    print("=" * 80)

    all_results: Dict[str, Dict[str, SectorResult]] = {}

    for sector_name, symbols_dict in SECTORS.items():
        print(f"\n{'─'*60}")
        print(f"  類股：{sector_name}（{len(symbols_dict)} 檔）")
        print(f"{'─'*60}")

        symbols = list(symbols_dict.keys())
        stock_data = fetch_tw_data(symbols, START_DATE, END_DATE)

        if not stock_data:
            print(f"  ⚠️ {sector_name} 無可用資料，跳過")
            continue

        sector_results = {}

        # A：純技術（門檻50，停利20%）
        t0 = time.time()
        r_base = run_portfolio_backtest(
            sector_name, stock_data, use_regime=False, mode_label="A_純技術")
        sector_results["A_純技術"] = r_base
        print(f"    A 完成（{time.time()-t0:.0f}s）報酬: {r_base.total_return_pct:+.1f}%  回撤: {r_base.max_drawdown_pct:.1f}%  勝率: {r_base.win_rate:.1f}%")

        # B：技術+盤勢（門檻50，停利20%）
        t0 = time.time()
        r_regime = run_portfolio_backtest(
            sector_name, stock_data, use_regime=True, mode_label="B_技術+盤勢")
        sector_results["B_技術+盤勢"] = r_regime
        print(f"    B 完成（{time.time()-t0:.0f}s）報酬: {r_regime.total_return_pct:+.1f}%  回撤: {r_regime.max_drawdown_pct:.1f}%  勝率: {r_regime.win_rate:.1f}%")

        # C：最佳化（門檻60，停利25%，技術+盤勢）
        t0 = time.time()
        r_opt = run_portfolio_backtest(
            sector_name, stock_data, use_regime=True,
            buy_threshold=60.0, sell_threshold=50.0,
            take_profit_pct=0.25,
            mode_label="C_最佳化")
        sector_results["C_最佳化"] = r_opt
        print(f"    C 完成（{time.time()-t0:.0f}s）報酬: {r_opt.total_return_pct:+.1f}%  回撤: {r_opt.max_drawdown_pct:.1f}%  勝率: {r_opt.win_rate:.1f}%")

        all_results[sector_name] = sector_results

    print_comparison(all_results)
    save_results(all_results)


if __name__ == "__main__":
    main()
