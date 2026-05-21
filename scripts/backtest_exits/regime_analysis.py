"""
按「個股 entry 時 RegimeLayer 判定」分組看每個策略的表現
特別關注：盤整段（多空雙殺）能不能賺錢
"""

from pathlib import Path

import pandas as pd

OUT_DIR = Path(__file__).resolve().parent / "out"

STRATS = ["S0_baseline", "S1_trailing_atr", "S2_ma_break", "S3_adaptive",
          "S4_regime_combo", "S6_either_2.0x", "S6_either_2.5x",
          "S7_adaptive_either", "S8_asymmetric"]

REGIMES_OF_INTEREST = ["強勢多頭", "多頭", "盤整", "高檔轉折", "空頭", "底部轉強"]


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
    for regime in REGIMES_OF_INTEREST + ["(no_regime)"]:
        for strat, df in trades.items():
            if regime == "(no_regime)":
                sub = df[df["stock_regime_at_entry"].isna()
                         | (df["stock_regime_at_entry"] == "")]
            else:
                sub = df[df["stock_regime_at_entry"] == regime]
            if sub.empty:
                rows.append({"regime": regime, "strategy": strat, "n": 0})
                continue
            wins = sub["pnl_pct"] > 0
            rows.append({
                "regime": regime,
                "strategy": strat,
                "n": len(sub),
                "win_rate_pct": round(wins.mean() * 100, 2),
                "avg_pnl_pct": round(sub["pnl_pct"].mean(), 2),
                "median_pnl_pct": round(sub["pnl_pct"].median(), 2),
                "total_pnl_pct": round(sub["pnl_pct"].sum(), 1),
                "worst_pnl_pct": round(sub["pnl_pct"].min(), 2),
                "avg_giveback_pct": round(sub["giveback_from_peak_pct"].mean(), 2),
                "false_sig_pct": round(sub["false_signal"].mean() * 100, 2),
                "avg_hold_days": round(sub["hold_days"].mean(), 1),
            })

    out_df = pd.DataFrame(rows)
    out_df.to_csv(OUT_DIR / "regime_summary.csv", index=False, encoding="utf-8-sig")

    for regime in REGIMES_OF_INTEREST:
        sub = out_df[out_df["regime"] == regime]
        if sub["n"].sum() == 0:
            print(f"\n== entry regime = {regime}: 無交易 ==")
            continue
        print(f"\n== 進場時個股 regime = {regime} ==")
        cols = ["strategy", "n", "win_rate_pct", "avg_pnl_pct", "total_pnl_pct",
                "worst_pnl_pct", "avg_giveback_pct", "false_sig_pct", "avg_hold_days"]
        print(sub[cols].to_string(index=False))

    print(f"\n寫入 {OUT_DIR / 'regime_summary.csv'}")
    return out_df


if __name__ == "__main__":
    analyze()
