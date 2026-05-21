"""
特定空頭/急殺段的策略表現分析
讀取 out/trades_*.csv 後，按指定日期段切出來算 metrics。
"""

from pathlib import Path

import pandas as pd

OUT_DIR = Path(__file__).resolve().parent / "out"

PERIODS = {
    "2022全年空頭": ("2022-01-01", "2022-12-31"),
    "2024/8急殺": ("2024-07-25", "2024-08-15"),
    "2025/4關稅段": ("2025-03-25", "2025-04-30"),
    "全部多頭段": ("2023-01-01", "2024-07-24"),  # 對照組
}

STRATS = ["S0_baseline", "S1_trailing_atr", "S6_either_2.0x",
          "S7_adaptive_either",
          # F3 變體
          "S0_baseline_f3", "S1_trailing_atr_f3", "S6_either_2.0x_f3",
          "S7_adaptive_either_f3", "S5_hybrid_3.0x_f3"]


def load_trades():
    out = {}
    for s in STRATS:
        p = OUT_DIR / f"trades_{s}.csv"
        if p.exists():
            df = pd.read_csv(p, parse_dates=["entry_date", "exit_date"])
            out[s] = df
    return out


def analyze():
    trades = load_trades()
    rows = []
    for period_name, (start, end) in PERIODS.items():
        s_dt = pd.Timestamp(start)
        e_dt = pd.Timestamp(end)
        for strat, df in trades.items():
            # 取出場日落在該段內的交易
            mask = (df["exit_date"] >= s_dt) & (df["exit_date"] <= e_dt)
            sub = df[mask]
            if sub.empty:
                rows.append({
                    "period": period_name, "strategy": strat, "n": 0,
                })
                continue
            wins = sub["pnl_pct"] > 0
            rows.append({
                "period": period_name,
                "strategy": strat,
                "n": len(sub),
                "win_rate_pct": round(wins.mean() * 100, 2),
                "avg_pnl_pct": round(sub["pnl_pct"].mean(), 2),
                "median_pnl_pct": round(sub["pnl_pct"].median(), 2),
                "worst_pnl_pct": round(sub["pnl_pct"].min(), 2),
                "avg_giveback_pct": round(sub["giveback_from_peak_pct"].mean(), 2),
                "median_giveback_pct": round(sub["giveback_from_peak_pct"].median(), 2),
                "max_giveback_pct": round(sub["giveback_from_peak_pct"].max(), 2),
                "false_sig_pct": round(sub["false_signal"].mean() * 100, 2),
                "avg_hold_days": round(sub["hold_days"].mean(), 1),
            })

    out_df = pd.DataFrame(rows)
    out_df.to_csv(OUT_DIR / "period_summary.csv", index=False, encoding="utf-8-sig")

    # Pretty print
    for period_name in PERIODS:
        sub = out_df[out_df["period"] == period_name]
        if sub["n"].sum() == 0:
            print(f"\n== {period_name}: 無交易 ==")
            continue
        print(f"\n== {period_name} ({PERIODS[period_name][0]} ~ {PERIODS[period_name][1]}) ==")
        cols = ["strategy", "n", "win_rate_pct", "avg_pnl_pct", "worst_pnl_pct",
                "avg_giveback_pct", "max_giveback_pct", "false_sig_pct", "avg_hold_days"]
        print(sub[cols].to_string(index=False))

    print(f"\n寫入 {OUT_DIR / 'period_summary.csv'}")
    return out_df


if __name__ == "__main__":
    analyze()
