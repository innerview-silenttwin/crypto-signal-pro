"""
指標歸因分析腳本 (Indicator Attribution Analysis)

讀取最新的 chipflow 回測 CSV（需含 entry_indicators 欄位），
統計每個技術指標在「有觸發該指標的交易」中的勝率與報酬表現，
輸出排名供調整權重參考。

使用方式：
    python backend/run_indicator_attribution.py
    python backend/run_indicator_attribution.py --file backend/data/backtest/chipflow_backtest_xxx.csv
    python backend/run_indicator_attribution.py --mode D_技術+盤勢+籌碼
"""

import sys
import os
import argparse
import glob
import pandas as pd


def load_latest_csv(data_dir: str) -> pd.DataFrame:
    """自動找最新的 chipflow 回測 CSV"""
    pattern = os.path.join(data_dir, "chipflow_backtest_*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"[ERROR] 找不到 chipflow 回測 CSV：{pattern}")
        sys.exit(1)
    path = files[-1]
    print(f"載入：{path}")
    return pd.read_csv(path)


def check_has_attribution(df: pd.DataFrame) -> bool:
    if "entry_indicators" not in df.columns:
        print()
        print("⚠️  此 CSV 不含 entry_indicators 欄位。")
        print("   請先執行最新版 run_chipflow_backtest.py 重新產生回測資料。")
        print()
        return False
    empty_pct = df["entry_indicators"].isna().mean() + (df["entry_indicators"] == "").mean()
    if empty_pct > 0.9:
        print(f"⚠️  entry_indicators 欄位有 {empty_pct*100:.0f}% 為空，歸因資料不足。")
        return False
    return True


def compute_attribution(df: pd.DataFrame) -> pd.DataFrame:
    """
    對每個指標統計：有觸發該指標的交易 vs. 沒有的差異

    回傳 DataFrame，欄位：
        indicator, count, win_rate, avg_pnl, avg_win, avg_loss,
        without_count, without_win_rate, without_avg_pnl, lift_win_rate, lift_avg_pnl
    """
    # 解析 entry_indicators（逗號分隔）
    df = df.copy()
    df["entry_indicators"] = df["entry_indicators"].fillna("")

    # 收集所有指標名稱
    all_indicators: set = set()
    for row in df["entry_indicators"]:
        for ind in str(row).split(","):
            ind = ind.strip()
            if ind:
                all_indicators.add(ind)

    records = []
    for ind in sorted(all_indicators):
        mask = df["entry_indicators"].str.contains(ind, regex=False, na=False)
        with_df = df[mask]
        without_df = df[~mask]

        if len(with_df) < 5:
            continue  # 樣本數太少，略過

        wins = (with_df["pnl_pct"] > 0).sum()
        total = len(with_df)
        win_rate = wins / total * 100
        avg_pnl = with_df["pnl_pct"].mean()
        avg_win = with_df.loc[with_df["pnl_pct"] > 0, "pnl_pct"].mean() if wins > 0 else float("nan")
        avg_loss = with_df.loc[with_df["pnl_pct"] <= 0, "pnl_pct"].mean() if (total - wins) > 0 else float("nan")

        wo_win_rate = (without_df["pnl_pct"] > 0).mean() * 100 if len(without_df) > 0 else float("nan")
        wo_avg_pnl = without_df["pnl_pct"].mean() if len(without_df) > 0 else float("nan")

        records.append({
            "indicator": ind,
            "count": total,
            "win_rate": round(win_rate, 1),
            "avg_pnl": round(avg_pnl, 2),
            "avg_win": round(avg_win, 2) if not pd.isna(avg_win) else None,
            "avg_loss": round(avg_loss, 2) if not pd.isna(avg_loss) else None,
            "wo_win_rate": round(wo_win_rate, 1) if not pd.isna(wo_win_rate) else None,
            "wo_avg_pnl": round(wo_avg_pnl, 2) if not pd.isna(wo_avg_pnl) else None,
            "lift_win_rate": round(win_rate - wo_win_rate, 1) if not pd.isna(wo_win_rate) else None,
            "lift_avg_pnl": round(avg_pnl - wo_avg_pnl, 2) if not pd.isna(wo_avg_pnl) else None,
        })

    result = pd.DataFrame(records)
    if result.empty:
        return result
    result = result.sort_values("avg_pnl", ascending=False).reset_index(drop=True)
    return result


def print_attribution_report(df_attr: pd.DataFrame, mode: str, sector: str = "全產業"):
    print()
    print(f"{'='*70}")
    print(f"  指標歸因分析 — {sector}  |  模式：{mode}")
    print(f"{'='*70}")
    if df_attr.empty:
        print("  無足夠資料可分析。")
        return

    header = f"{'指標名稱':<22} {'筆數':>5} {'勝率':>7} {'平均報酬':>9} {'平均獲利':>9} {'平均虧損':>9} {'勝率提升':>8} {'報酬提升':>8}"
    print(header)
    print("-" * 80)
    for _, row in df_attr.iterrows():
        lift_wr = f"{row['lift_win_rate']:+.1f}%" if row["lift_win_rate"] is not None else "  —"
        lift_pnl = f"{row['lift_avg_pnl']:+.2f}%" if row["lift_avg_pnl"] is not None else "  —"
        avg_win = f"{row['avg_win']:+.1f}%" if row["avg_win"] is not None else "  —"
        avg_loss = f"{row['avg_loss']:+.1f}%" if row["avg_loss"] is not None else "  —"
        print(
            f"  {row['indicator']:<20} {row['count']:>5} "
            f" {row['win_rate']:>5.1f}%  {row['avg_pnl']:>+7.2f}%"
            f"  {avg_win:>8}  {avg_loss:>8}"
            f"  {lift_wr:>8}  {lift_pnl:>8}"
        )
    print()
    print("  * 勝率提升／報酬提升 = 有該指標的交易 vs. 沒有該指標的交易之差值")
    print("  * 正值代表該指標出現時，交易表現比平均更好")


def print_score_distribution(df: pd.DataFrame, mode: str):
    """顯示進場分數分佈（每 10 分一個區間）"""
    if "entry_score" not in df.columns:
        return
    df = df.copy()
    df["score_bin"] = (df["entry_score"] // 10 * 10).astype(int)
    print(f"\n{'─'*50}")
    print(f"  進場分數分佈  |  模式：{mode}")
    print(f"{'─'*50}")
    print(f"  {'分數區間':<12} {'筆數':>5} {'勝率':>7} {'平均報酬':>9}")
    for bin_val, grp in df.groupby("score_bin"):
        wr = (grp["pnl_pct"] > 0).mean() * 100
        avg = grp["pnl_pct"].mean()
        print(f"  {bin_val}–{bin_val+9}分       {len(grp):>5}  {wr:>5.1f}%  {avg:>+7.2f}%")
    print()


def main():
    parser = argparse.ArgumentParser(description="指標歸因分析")
    parser.add_argument("--file", default=None, help="指定回測 CSV 路徑")
    parser.add_argument("--mode", default=None, help="篩選特定模式，e.g. D_技術+盤勢+籌碼")
    parser.add_argument("--sector", default=None, help="篩選特定產業，e.g. 半導體")
    args = parser.parse_args()

    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest")

    if args.file:
        print(f"載入：{args.file}")
        df_all = pd.read_csv(args.file)
    else:
        df_all = load_latest_csv(data_dir)

    print(f"共 {len(df_all)} 筆交易紀錄，欄位：{df_all.columns.tolist()}")

    if not check_has_attribution(df_all):
        sys.exit(0)

    modes = [args.mode] if args.mode else df_all["mode"].unique().tolist()
    sectors = [args.sector] if args.sector else ["全產業"]

    for mode in modes:
        df_mode = df_all[df_all["mode"] == mode] if mode in df_all["mode"].values else df_all

        # 全產業
        if "全產業" in sectors:
            df_attr = compute_attribution(df_mode)
            print_attribution_report(df_attr, mode, "全產業")
            print_score_distribution(df_mode, mode)

        # 各產業分開
        if len(sectors) == 1 and sectors[0] != "全產業":
            sec = sectors[0]
            df_sec = df_mode[df_mode["sector"] == sec]
            df_attr = compute_attribution(df_sec)
            print_attribution_report(df_attr, mode, sec)
            print_score_distribution(df_sec, mode)
        elif "全產業" in sectors and "sector" in df_all.columns:
            # 也印各產業分開
            for sec in df_mode["sector"].unique():
                df_sec = df_mode[df_mode["sector"] == sec]
                df_attr = compute_attribution(df_sec)
                print_attribution_report(df_attr, mode, sec)


if __name__ == "__main__":
    main()
