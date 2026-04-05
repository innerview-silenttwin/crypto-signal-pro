"""
BTC 策略回測：比較有/無 CryptoFlowLayer 的效果

對比：
A) 純技術面（原有 7 指標）
B) 技術面 + CryptoFlowLayer（恐懼貪婪 + 資金費率）
C) 技術面 + RegimeLayer + CryptoFlowLayer（完整三層）
"""

import sys
import os
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine, BacktestResult
from layers.regime import RegimeLayer
from layers.crypto_flow import CryptoFlowLayer


def load_data(filename: str) -> pd.DataFrame:
    filepath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", filename)
    df = pd.read_csv(filepath, index_col="timestamp", parse_dates=True)
    print(f"   📂 載入: {filename} ({len(df)} bars, {df.index[0].date()} ~ {df.index[-1].date()})")
    return df


def run_comparison():
    print("=" * 80)
    print("🚀 BTC 策略比較回測：純技術 vs 技術+CryptoFlow vs 完整三層")
    print("=" * 80)

    daily_data = load_data("btc_daily_7y.csv")
    h4_data = load_data("btc_4h_2y.csv")

    # 分析層
    regime_layer = RegimeLayer()
    crypto_flow_layer = CryptoFlowLayer()

    layer_configs = [
        ("純技術面", None),
        ("技術+CryptoFlow", [crypto_flow_layer]),
        ("技術+Regime+CryptoFlow", [regime_layer, crypto_flow_layer]),
    ]

    # 最佳場景組合
    scenarios = [
        # (名稱, 數據, timeframe, buy_th, sell_th, sl%, tp%)
        ("日線-門檻40", daily_data, "1d", 40, 40, 15.0, 30.0),
        ("日線-門檻35", daily_data, "1d", 35, 35, 12.0, 25.0),
        ("日線-門檻45", daily_data, "1d", 45, 45, 12.0, 25.0),
        ("4H-門檻35", h4_data, "4h", 35, 35, 8.0, 20.0),
        ("4H-門檻40", h4_data, "4h", 40, 40, 6.0, 15.0),
    ]

    all_results = []

    for s_name, data, tf, buy_th, sell_th, sl, tp in scenarios:
        for l_name, layers in layer_configs:
            tag = f"{s_name} | {l_name}"
            print(f"\n{'─' * 80}")
            print(f"📋 {tag}")
            print(f"{'─' * 80}")

            engine = BacktestEngine()
            result = engine.run(
                df=data,
                symbol="BTC/USDT",
                timeframe=tf,
                buy_threshold=buy_th,
                sell_threshold=sell_th,
                stop_loss_pct=sl,
                take_profit_pct=tp,
                layers=layers,
            )
            all_results.append((s_name, l_name, result))

    # === 綜合比較表 ===
    print("\n" + "=" * 120)
    print("📊 綜合比較報告")
    print("=" * 120)

    header = (
        f"{'場景':<16} {'分析層':<24} {'交易數':>6} {'勝率':>8} "
        f"{'累計獲利':>10} {'最大回撤':>10} {'夏普':>8} {'獲利因子':>10} {'平均持倉':>10}"
    )
    print(header)
    print("-" * 120)

    for s_name, l_name, r in all_results:
        line = (
            f"{s_name:<16} {l_name:<24} {r.total_trades:>6} {r.win_rate:>7.1f}% "
            f"{r.total_profit_pct:>+9.2f}% {r.max_drawdown_pct:>9.2f}% "
            f"{r.sharpe_ratio:>8.2f} {r.profit_factor:>10.2f} {r.avg_holding_days:>8.1f}天"
        )
        print(line)

    # === 按累計獲利排序前 5 ===
    print("\n" + "=" * 80)
    print("🏆 Top 5 策略（按累計獲利排序）")
    print("=" * 80)

    sorted_results = sorted(all_results, key=lambda x: x[2].total_profit_pct, reverse=True)
    for i, (s_name, l_name, r) in enumerate(sorted_results[:5], 1):
        print(
            f"  #{i} {s_name} + {l_name}: "
            f"{r.total_profit_pct:+.2f}% | 勝率 {r.win_rate:.1f}% | "
            f"Sharpe {r.sharpe_ratio:.2f} | PF {r.profit_factor:.2f} | "
            f"{r.total_trades} 筆交易"
        )

    # === 儲存報告 ===
    report_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "btc_flow_backtest_report.txt"
    )
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"BTC 策略比較回測報告\n")
        f.write(f"產生時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"{'=' * 120}\n\n")

        f.write(header + "\n")
        f.write("-" * 120 + "\n")
        for s_name, l_name, r in all_results:
            line = (
                f"{s_name:<16} {l_name:<24} {r.total_trades:>6} {r.win_rate:>7.1f}% "
                f"{r.total_profit_pct:>+9.2f}% {r.max_drawdown_pct:>9.2f}% "
                f"{r.sharpe_ratio:>8.2f} {r.profit_factor:>10.2f} {r.avg_holding_days:>8.1f}天"
            )
            f.write(line + "\n")

        f.write(f"\n{'=' * 80}\n")
        f.write("Top 5 策略\n")
        f.write(f"{'=' * 80}\n")
        for i, (s_name, l_name, r) in enumerate(sorted_results[:5], 1):
            f.write(
                f"  #{i} {s_name} + {l_name}: "
                f"{r.total_profit_pct:+.2f}% | 勝率 {r.win_rate:.1f}% | "
                f"Sharpe {r.sharpe_ratio:.2f} | PF {r.profit_factor:.2f} | "
                f"{r.total_trades} 筆\n"
            )

        # 每個場景的詳細報告
        f.write(f"\n\n{'=' * 80}\n")
        f.write("詳細回測報告\n")
        f.write(f"{'=' * 80}\n")
        for s_name, l_name, r in all_results:
            f.write(f"\n--- {s_name} | {l_name} ---\n")
            f.write(r.report())
            f.write("\n")

    print(f"\n💾 報告已儲存: {report_path}")
    print("\n✅ 比較回測完成！")

    return all_results


if __name__ == "__main__":
    run_comparison()
