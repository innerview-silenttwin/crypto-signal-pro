"""
台股類股技術指標組合回測

功能：
1. 透過 yfinance 下載各類股代表性個股的 7 年日線數據
2. 測試多種技術指標權重組合
3. 找出每個類股最適合的技術指標組合
4. 產出完整回測報告
"""

import sys
import os
import time
import warnings
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
from itertools import combinations

import pandas as pd
import numpy as np
import yfinance as yf

warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine, BacktestResult
from signals.aggregator import SignalAggregator, MarketType


# ═══════════════════════════════════════════════════════════════
# 類股定義
# ═══════════════════════════════════════════════════════════════

SECTORS = {
    "半導體": {
        "stocks": {
            "2330.TW": "台積電",
            "2454.TW": "聯發科",
            "2303.TW": "聯電",
            "3711.TW": "日月光投控",
            "2379.TW": "瑞昱",
        },
        "description": "半導體產業鏈：晶圓代工、IC 設計、封測",
    },
    "電子代工/零組件": {
        "stocks": {
            "2317.TW": "鴻海",
            "2382.TW": "廣達",
            "2308.TW": "台達電",
            "2357.TW": "華碩",
            "3008.TW": "大立光",
        },
        "description": "電子代工、零組件、光電",
    },
    "金融": {
        "stocks": {
            "2881.TW": "富邦金",
            "2882.TW": "國泰金",
            "2891.TW": "中信金",
            "2886.TW": "兆豐金",
            "2884.TW": "玉山金",
        },
        "description": "金控、銀行、保險、證券",
    },
    "傳產/航運": {
        "stocks": {
            "1301.TW": "台塑",
            "2002.TW": "中鋼",
            "1216.TW": "統一",
            "2603.TW": "長榮",
            "2412.TW": "中華電",
        },
        "description": "塑化、鋼鐵、食品、航運、電信",
    },
}


# ═══════════════════════════════════════════════════════════════
# 技術指標組合定義
# ═══════════════════════════════════════════════════════════════

# 每個組合的 7 個指標權重加總 = 100
INDICATOR_COMBOS = {
    "動能主導 (RSI+MACD)": {
        'rsi': 25.0, 'macd': 25.0, 'bollinger': 10.0,
        'mfi': 10.0, 'ema_cross': 15.0, 'volume': 10.0, 'adx': 5.0,
    },
    "量能主導 (Vol+MFI)": {
        'rsi': 10.0, 'macd': 10.0, 'bollinger': 10.0,
        'mfi': 25.0, 'ema_cross': 10.0, 'volume': 30.0, 'adx': 5.0,
    },
    "趨勢追蹤 (EMA+ADX)": {
        'rsi': 10.0, 'macd': 15.0, 'bollinger': 5.0,
        'mfi': 5.0, 'ema_cross': 30.0, 'volume': 10.0, 'adx': 25.0,
    },
    "均值回歸 (BB+RSI)": {
        'rsi': 25.0, 'macd': 10.0, 'bollinger': 30.0,
        'mfi': 10.0, 'ema_cross': 5.0, 'volume': 10.0, 'adx': 10.0,
    },
    "量價共振 (Vol+MACD+MFI)": {
        'rsi': 10.0, 'macd': 25.0, 'bollinger': 5.0,
        'mfi': 20.0, 'ema_cross': 10.0, 'volume': 25.0, 'adx': 5.0,
    },
    "全面均衡": {
        'rsi': 15.0, 'macd': 15.0, 'bollinger': 15.0,
        'mfi': 15.0, 'ema_cross': 15.0, 'volume': 15.0, 'adx': 10.0,
    },
    "趨勢+量能 (EMA+ADX+Vol)": {
        'rsi': 5.0, 'macd': 10.0, 'bollinger': 5.0,
        'mfi': 10.0, 'ema_cross': 25.0, 'volume': 25.0, 'adx': 20.0,
    },
    "動能+趨勢 (RSI+MACD+EMA)": {
        'rsi': 20.0, 'macd': 25.0, 'bollinger': 5.0,
        'mfi': 5.0, 'ema_cross': 25.0, 'volume': 10.0, 'adx': 10.0,
    },
    "波動突破 (BB+Vol+ADX)": {
        'rsi': 5.0, 'macd': 10.0, 'bollinger': 25.0,
        'mfi': 10.0, 'ema_cross': 10.0, 'volume': 20.0, 'adx': 20.0,
    },
    "資金流向 (MFI+RSI+Vol)": {
        'rsi': 20.0, 'macd': 10.0, 'bollinger': 10.0,
        'mfi': 25.0, 'ema_cross': 5.0, 'volume': 25.0, 'adx': 5.0,
    },
}

# 回測參數組合
BACKTEST_PARAMS = [
    # (名稱, 買入門檻, 賣出門檻, 停損%, 停利%)
    ("標準", 40, 40, 8.0, 20.0),
    ("寬鬆", 30, 30, 10.0, 25.0),
    ("嚴格", 50, 50, 6.0, 15.0),
]


# ═══════════════════════════════════════════════════════════════
# 數據下載
# ═══════════════════════════════════════════════════════════════

def fetch_stock_data(symbol: str, years: int = 7) -> Optional[pd.DataFrame]:
    """用 yfinance 下載台股歷史數據"""
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'history', 'backtest_cache')
    os.makedirs(cache_dir, exist_ok=True)

    cache_file = os.path.join(cache_dir, f"{symbol.replace('.', '_')}_{years}y.csv")

    # 檢查快取（1天內的快取視為有效）
    if os.path.exists(cache_file):
        mtime = os.path.getmtime(cache_file)
        if time.time() - mtime < 86400:
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            if len(df) > 200:
                print(f"   📂 快取載入: {symbol} ({len(df)} 筆)")
                return df

    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=years * 365)

        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_date.strftime('%Y-%m-%d'),
                           end=end_date.strftime('%Y-%m-%d'),
                           interval='1d')

        if df.empty or len(df) < 200:
            print(f"   ⚠️ {symbol} 數據不足 ({len(df)} 筆)，跳過")
            return None

        # 標準化欄位名
        df.columns = [c.lower() for c in df.columns]
        # 確保有必要欄位
        required = ['open', 'high', 'low', 'close', 'volume']
        for col in required:
            if col not in df.columns:
                print(f"   ⚠️ {symbol} 缺少 {col} 欄位")
                return None

        df = df[required]
        df = df.dropna()
        df = df[df['volume'] > 0]  # 移除零成交量的日子

        # 儲存快取
        df.to_csv(cache_file)
        print(f"   ✅ 下載完成: {symbol} ({len(df)} 筆, {df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')})")
        return df

    except Exception as e:
        print(f"   ❌ {symbol} 下載失敗: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# 單股回測
# ═══════════════════════════════════════════════════════════════

@dataclass
class ComboResult:
    """單一組合的回測結果"""
    combo_name: str
    param_name: str
    weights: Dict[str, float]
    total_profit_pct: float = 0.0
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    total_trades: int = 0
    avg_holding_days: float = 0.0
    # 用於綜合評分
    composite_score: float = 0.0


def precompute_indicators(df: pd.DataFrame, weights: Dict[str, float]) -> pd.DataFrame:
    """預先計算所有技術指標（一次性，大幅加速）"""
    aggregator = SignalAggregator(weights=weights)
    df_computed = aggregator.calculate_all(df.copy())
    return df_computed, aggregator


def run_single_backtest_fast(
    df_computed: pd.DataFrame,
    aggregator: SignalAggregator,
    symbol: str,
    buy_threshold: float,
    sell_threshold: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> Optional[BacktestResult]:
    """對單一股票執行回測（使用預計算指標，大幅加速）"""
    try:
        from backtest.engine import Trade, BacktestResult as BR

        trades = []
        current_trade = None
        buy_signals_count = 0
        sell_signals_count = 0
        peak_equity = 100.0
        max_drawdown = 0.0

        start_idx = 200

        for i in range(start_idx, len(df_computed)):
            current_time = df_computed.index[i]
            current_price = df_computed['close'].iloc[i]

            # 只取到當前 bar 的 slice，generate_signals 只讀最後一行
            window = df_computed.iloc[max(0, i - 50):i + 1]

            try:
                signal = aggregator.generate_signals(window, symbol, "1d")
            except Exception:
                continue

            if signal.direction == "BUY" and signal.confidence >= buy_threshold:
                buy_signals_count += 1
            elif signal.direction == "SELL" and signal.confidence >= sell_threshold:
                sell_signals_count += 1

            if current_trade is None:
                if signal.direction == "BUY" and signal.confidence >= buy_threshold:
                    current_trade = Trade(
                        entry_time=current_time,
                        entry_price=current_price,
                        entry_score=signal.confidence,
                        entry_reason="",
                        direction="BUY",
                    )
            else:
                price_change_pct = (current_price - current_trade.entry_price) / current_trade.entry_price * 100
                should_close = False
                close_reason = ""

                if price_change_pct <= -stop_loss_pct:
                    should_close = True
                    close_reason = "停損"
                elif price_change_pct >= take_profit_pct:
                    should_close = True
                    close_reason = "停利"
                elif signal.direction == "SELL" and signal.confidence >= sell_threshold:
                    should_close = True
                    close_reason = "賣出信號"

                if should_close:
                    current_trade.close(current_time, current_price, signal.confidence, close_reason)
                    trades.append(current_trade)
                    current_trade = None

            if trades:
                cumulative = 100.0
                for t in trades:
                    cumulative *= (1 + t.profit_pct / 100)
                peak_equity = max(peak_equity, cumulative)
                drawdown = (peak_equity - cumulative) / peak_equity * 100
                max_drawdown = max(max_drawdown, drawdown)

        if current_trade is not None:
            current_trade.close(df_computed.index[-1], df_computed['close'].iloc[-1], 0, "回測結束平倉")
            trades.append(current_trade)

        result = BR(
            symbol=symbol, timeframe="1d",
            period=f"{df_computed.index[0].strftime('%Y-%m-%d')} ~ {df_computed.index[-1].strftime('%Y-%m-%d')}",
            signal_threshold=buy_threshold,
            trades=trades,
            total_buy_signals=buy_signals_count,
            total_sell_signals=sell_signals_count,
        )

        if trades:
            profits = [t.profit_pct for t in trades]
            wins = [p for p in profits if p > 0]
            losses = [p for p in profits if p <= 0]

            result.total_trades = len(trades)
            result.winning_trades = len(wins)
            result.losing_trades = len(losses)
            result.win_rate = len(wins) / len(trades) * 100 if trades else 0
            result.total_profit_pct = sum(profits)
            result.avg_profit_pct = np.mean(profits) if profits else 0
            result.avg_win_pct = np.mean(wins) if wins else 0
            result.avg_loss_pct = np.mean(losses) if losses else 0
            result.max_profit_pct = max(profits) if profits else 0
            result.max_loss_pct = min(profits) if profits else 0
            result.max_drawdown_pct = max_drawdown
            result.avg_holding_days = np.mean([t.holding_days for t in trades])

            if len(profits) > 1:
                result.sharpe_ratio = np.mean(profits) / np.std(profits) if np.std(profits) > 0 else 0

            total_wins = sum(wins) if wins else 0
            total_losses = abs(sum(losses)) if losses else 0
            result.profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')

        return result
    except Exception as e:
        return None


def calculate_composite_score(result: BacktestResult) -> float:
    """
    計算綜合評分
    權衡：獲利、勝率、風險調節後收益、最大回撤
    """
    if result.total_trades < 3:
        return -999  # 交易次數太少，不具統計意義

    # 各指標正規化後加權
    profit_score = result.total_profit_pct * 0.30  # 獲利佔 30%
    winrate_score = result.win_rate * 0.20          # 勝率佔 20%
    sharpe_score = result.sharpe_ratio * 30 * 0.25  # 夏普比率佔 25%
    pf_score = min(result.profit_factor, 5) * 10 * 0.15  # 獲利因子佔 15%（上限 5）
    dd_penalty = result.max_drawdown_pct * 0.10     # 最大回撤扣分 10%

    return profit_score + winrate_score + sharpe_score + pf_score - dd_penalty


# ═══════════════════════════════════════════════════════════════
# 類股回測主流程
# ═══════════════════════════════════════════════════════════════

def run_sector_backtest():
    """執行全部類股回測"""

    print("=" * 80)
    print("🚀 台股類股技術指標組合回測系統")
    print(f"   回測期間: 近 7 年 | 時間框架: 日線")
    print(f"   類股數量: {len(SECTORS)} | 指標組合: {len(INDICATOR_COMBOS)} | 參數組合: {len(BACKTEST_PARAMS)}")
    print(f"   產生時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    # 結果儲存：sector -> list of (combo_name, param_name, avg_composite_score, detail)
    sector_results: Dict[str, List[ComboResult]] = {}
    # 個股詳細結果
    stock_detail_results: Dict[str, Dict[str, List[Tuple[str, str, BacktestResult]]]] = {}

    total_tests = 0

    for sector_name, sector_info in SECTORS.items():
        print(f"\n\n{'█' * 80}")
        print(f"█  類股: {sector_name} — {sector_info['description']}")
        print(f"{'█' * 80}")

        sector_results[sector_name] = []
        stock_detail_results[sector_name] = {}

        # 1. 下載所有股票數據
        stock_data: Dict[str, pd.DataFrame] = {}
        for symbol, name in sector_info['stocks'].items():
            print(f"\n📡 下載 {name} ({symbol})...")
            df = fetch_stock_data(symbol, years=7)
            if df is not None:
                stock_data[symbol] = df

        if not stock_data:
            print(f"   ⚠️ {sector_name} 無可用數據，跳過")
            continue

        print(f"\n   可用股票: {len(stock_data)}/{len(sector_info['stocks'])}")

        # 2. 對每個指標組合 × 參數組合 × 個股 進行回測
        for combo_name, weights in INDICATOR_COMBOS.items():
            # 預計算：每個 stock+combo 只算一次指標（大幅加速）
            precomputed = {}
            for symbol, df in stock_data.items():
                try:
                    df_comp, agg = precompute_indicators(df, weights)
                    precomputed[symbol] = (df_comp, agg)
                except Exception:
                    pass

            print(f"   ▸ {combo_name} (指標已預算, {len(precomputed)} 股)")

            for param_name, buy_th, sell_th, sl, tp in BACKTEST_PARAMS:

                combo_scores = []
                combo_profits = []
                combo_winrates = []
                combo_sharpes = []
                combo_pfs = []
                combo_dds = []
                combo_trades_total = []
                combo_holdings = []

                full_key = f"{combo_name}|{param_name}"

                for symbol in precomputed:
                    stock_name = sector_info['stocks'][symbol]
                    total_tests += 1
                    df_comp, agg = precomputed[symbol]

                    result = run_single_backtest_fast(
                        df_comp, agg, symbol, buy_th, sell_th, sl, tp
                    )

                    if result is None:
                        continue

                    # 記錄個股結果
                    if symbol not in stock_detail_results[sector_name]:
                        stock_detail_results[sector_name][symbol] = []
                    stock_detail_results[sector_name][symbol].append(
                        (combo_name, param_name, result)
                    )

                    score = calculate_composite_score(result)
                    combo_scores.append(score)
                    combo_profits.append(result.total_profit_pct)
                    combo_winrates.append(result.win_rate)
                    combo_sharpes.append(result.sharpe_ratio)
                    combo_pfs.append(result.profit_factor)
                    combo_dds.append(result.max_drawdown_pct)
                    combo_trades_total.append(result.total_trades)
                    combo_holdings.append(result.avg_holding_days)

                if combo_scores:
                    cr = ComboResult(
                        combo_name=combo_name,
                        param_name=param_name,
                        weights=weights,
                        total_profit_pct=np.mean(combo_profits),
                        win_rate=np.mean(combo_winrates),
                        sharpe_ratio=np.mean(combo_sharpes),
                        profit_factor=np.mean(combo_pfs),
                        max_drawdown_pct=np.mean(combo_dds),
                        total_trades=int(np.mean(combo_trades_total)),
                        avg_holding_days=np.mean(combo_holdings),
                        composite_score=np.mean(combo_scores),
                    )
                    sector_results[sector_name].append(cr)

        # 3. 排序，找出此類股最佳組合
        sector_results[sector_name].sort(key=lambda x: x.composite_score, reverse=True)

        if sector_results[sector_name]:
            best = sector_results[sector_name][0]
            print(f"\n   🏆 {sector_name} 最佳組合: {best.combo_name} ({best.param_name})")
            print(f"      綜合評分: {best.composite_score:.1f} | 累計獲利: {best.total_profit_pct:+.2f}%")
            print(f"      勝率: {best.win_rate:.1f}% | 夏普: {best.sharpe_ratio:.2f}")

    print(f"\n\n✅ 回測完成！共執行 {total_tests} 次回測")

    # ── 產出報告 ──
    report = generate_report(sector_results, stock_detail_results)

    report_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'sector_backtest_report.txt'
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"\n💾 報告已儲存: {report_path}")
    return sector_results


# ═══════════════════════════════════════════════════════════════
# 報告產生
# ═══════════════════════════════════════════════════════════════

def generate_report(
    sector_results: Dict[str, List[ComboResult]],
    stock_detail_results: Dict[str, Dict[str, List[Tuple[str, str, BacktestResult]]]],
) -> str:
    """產出完整回測報告"""

    lines = []
    lines.append("台股類股技術指標組合回測報告")
    lines.append(f"產生時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"回測期間: 近 7 年 (日線)")
    lines.append(f"指標組合數: {len(INDICATOR_COMBOS)} | 參數組合數: {len(BACKTEST_PARAMS)}")
    lines.append("")

    # ── 總覽：各類股最佳組合 ──
    lines.append("=" * 80)
    lines.append("📊 總覽：各類股最佳技術指標組合")
    lines.append("=" * 80)
    lines.append("")

    for sector_name, results in sector_results.items():
        if not results:
            continue
        best = results[0]
        lines.append(f"▸ {sector_name}")
        lines.append(f"  最佳組合: {best.combo_name} ({best.param_name})")
        lines.append(f"  綜合評分: {best.composite_score:.1f}")
        lines.append(f"  平均累計獲利: {best.total_profit_pct:+.2f}% | 勝率: {best.win_rate:.1f}%")
        lines.append(f"  夏普比率: {best.sharpe_ratio:.2f} | 獲利因子: {best.profit_factor:.2f}")
        lines.append(f"  最大回撤: {best.max_drawdown_pct:.2f}% | 平均持倉: {best.avg_holding_days:.1f} 天")
        lines.append(f"  權重配置: {_format_weights(best.weights)}")
        lines.append("")

    # ── 各類股詳細報告 ──
    for sector_name, results in sector_results.items():
        if not results:
            continue

        lines.append("")
        lines.append("█" * 80)
        lines.append(f"█  類股詳細報告: {sector_name}")
        lines.append(f"█  {SECTORS[sector_name]['description']}")
        lines.append("█" * 80)

        # Top 5 組合
        lines.append("")
        lines.append("─" * 80)
        lines.append(f"🏆 {sector_name} — Top 5 最佳指標組合")
        lines.append("─" * 80)

        for rank, cr in enumerate(results[:5], 1):
            lines.append("")
            lines.append(f"  第 {rank} 名: {cr.combo_name} ({cr.param_name})")
            lines.append("  " + "=" * 70)
            lines.append(f"  綜合評分:     {cr.composite_score:.1f}")
            lines.append(f"  ")
            lines.append(f"  【交易統計】")
            lines.append(f"    平均交易次數: {cr.total_trades}")
            lines.append(f"    ✅ 平均勝率:   {cr.win_rate:.1f}%")
            lines.append(f"  ")
            lines.append(f"  【獲利表現】")
            lines.append(f"    平均累計獲利: {cr.total_profit_pct:+.2f}%")
            lines.append(f"  ")
            lines.append(f"  【風險指標】")
            lines.append(f"    平均最大回撤: {cr.max_drawdown_pct:.2f}%")
            lines.append(f"    平均夏普比率: {cr.sharpe_ratio:.2f}")
            lines.append(f"    平均獲利因子: {cr.profit_factor:.2f}")
            lines.append(f"  ")
            lines.append(f"  【持倉統計】")
            lines.append(f"    平均持倉天數: {cr.avg_holding_days:.1f} 天")
            lines.append(f"  ")
            lines.append(f"  【指標權重】")
            lines.append(f"    {_format_weights(cr.weights)}")
            lines.append("  " + "=" * 70)

        # Worst 3 組合
        lines.append("")
        lines.append("─" * 80)
        lines.append(f"⚠️  {sector_name} — 最不適合的 3 個組合")
        lines.append("─" * 80)

        for rank, cr in enumerate(results[-3:], 1):
            lines.append(f"  ✘ {cr.combo_name} ({cr.param_name})")
            lines.append(f"    綜合評分: {cr.composite_score:.1f} | "
                        f"獲利: {cr.total_profit_pct:+.2f}% | "
                        f"勝率: {cr.win_rate:.1f}% | "
                        f"最大回撤: {cr.max_drawdown_pct:.2f}%")

        # 個股表現（使用最佳組合）
        if sector_name in stock_detail_results:
            best_combo = results[0].combo_name
            best_param = results[0].param_name

            lines.append("")
            lines.append("─" * 80)
            lines.append(f"📈 {sector_name} 個股表現 (使用最佳組合: {best_combo} | {best_param})")
            lines.append("─" * 80)

            for symbol, detail_list in stock_detail_results[sector_name].items():
                stock_name = SECTORS[sector_name]['stocks'].get(symbol, symbol)
                # 找到最佳組合的結果
                for combo_n, param_n, result in detail_list:
                    if combo_n == best_combo and param_n == best_param:
                        lines.append("")
                        lines.append(f"  ▸ {stock_name} ({symbol})")
                        lines.append(f"    {result.period}")
                        lines.append(f"    交易次數: {result.total_trades} | "
                                    f"勝率: {result.win_rate:.1f}% | "
                                    f"累計獲利: {result.total_profit_pct:+.2f}%")
                        lines.append(f"    夏普比率: {result.sharpe_ratio:.2f} | "
                                    f"獲利因子: {result.profit_factor:.2f} | "
                                    f"最大回撤: {result.max_drawdown_pct:.2f}%")
                        lines.append(f"    平均持倉: {result.avg_holding_days:.1f} 天 | "
                                    f"最大單筆獲利: {result.max_profit_pct:+.2f}% | "
                                    f"最大單筆虧損: {result.max_loss_pct:+.2f}%")
                        break

    # ── 跨類股比較 ──
    lines.append("")
    lines.append("")
    lines.append("=" * 80)
    lines.append("📊 跨類股指標組合適用性矩陣")
    lines.append("=" * 80)
    lines.append("")

    # 建立矩陣: combo -> sector -> best_score
    combo_sector_matrix: Dict[str, Dict[str, float]] = {}
    for sector_name, results in sector_results.items():
        for cr in results:
            key = cr.combo_name
            if key not in combo_sector_matrix:
                combo_sector_matrix[key] = {}
            # 取該combo在此sector下最好的param結果
            if sector_name not in combo_sector_matrix[key] or cr.composite_score > combo_sector_matrix[key][sector_name]:
                combo_sector_matrix[key][sector_name] = cr.composite_score

    sector_names = list(sector_results.keys())
    header = f"{'指標組合':<28}" + "".join(f"{s:<16}" for s in sector_names) + "平均"
    lines.append(header)
    lines.append("-" * len(header))

    combo_avgs = []
    for combo_name, sector_scores in sorted(combo_sector_matrix.items()):
        row = f"{combo_name:<28}"
        scores = []
        for s in sector_names:
            score = sector_scores.get(s, 0)
            scores.append(score)
            row += f"{score:>10.1f}      "
        avg = np.mean(scores) if scores else 0
        combo_avgs.append((combo_name, avg))
        row += f"{avg:>8.1f}"
        lines.append(row)

    # ── 最終建議 ──
    lines.append("")
    lines.append("")
    lines.append("=" * 80)
    lines.append("🏆 最終建議")
    lines.append("=" * 80)
    lines.append("")

    # 全市場最佳組合
    combo_avgs.sort(key=lambda x: x[1], reverse=True)
    lines.append(f"📌 全市場最佳通用組合: {combo_avgs[0][0]} (平均分: {combo_avgs[0][1]:.1f})")
    lines.append("")

    lines.append("📌 各類股專屬建議:")
    for sector_name, results in sector_results.items():
        if results:
            best = results[0]
            lines.append(f"   ▸ {sector_name}: 使用「{best.combo_name}」({best.param_name})")
            lines.append(f"     預期獲利: {best.total_profit_pct:+.2f}% | 勝率: {best.win_rate:.1f}% | 夏普: {best.sharpe_ratio:.2f}")
            # 指出關鍵指標
            top_indicators = sorted(best.weights.items(), key=lambda x: x[1], reverse=True)[:3]
            indicator_names = {
                'rsi': 'RSI', 'macd': 'MACD', 'bollinger': '布林通道',
                'mfi': 'MFI資金流', 'ema_cross': 'EMA交叉', 'volume': '成交量', 'adx': 'ADX趨勢'
            }
            top_str = "、".join([f"{indicator_names.get(k, k)}({v:.0f})" for k, v in top_indicators])
            lines.append(f"     關鍵指標: {top_str}")
            lines.append("")

    lines.append("")
    lines.append("=" * 80)
    lines.append("📝 附註")
    lines.append("=" * 80)
    lines.append("• 綜合評分 = 獲利(30%) + 勝率(20%) + 夏普比率(25%) + 獲利因子(15%) - 最大回撤(10%)")
    lines.append("• 交易次數不足 3 次的組合不列入評比")
    lines.append("• 各指標的回測使用日線數據，適用於中長期波段操作")
    lines.append("• 過去績效不代表未來表現，建議搭配基本面分析與市場環境判斷")
    lines.append("• 停損/停利設定會顯著影響回測結果，實際交易應根據個人風險承受度調整")
    lines.append("")

    return "\n".join(lines)


def _format_weights(weights: Dict[str, float]) -> str:
    """格式化權重為可讀字串"""
    names = {
        'rsi': 'RSI', 'macd': 'MACD', 'bollinger': 'BB',
        'mfi': 'MFI', 'ema_cross': 'EMA', 'volume': 'Vol', 'adx': 'ADX'
    }
    parts = [f"{names.get(k, k)}:{v:.0f}" for k, v in sorted(weights.items(), key=lambda x: -x[1])]
    return " | ".join(parts)


# ═══════════════════════════════════════════════════════════════
# 主程式入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    results = run_sector_backtest()
