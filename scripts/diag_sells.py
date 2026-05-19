#!/usr/bin/env python3
"""diag_sells.py — 列出近 N 天所有 SELL 紀錄，標出哪些是「lag 出場」。

判斷 lag：
  - 訊號標籤是「停損觸發 (-X%)」
  - 但實際 profit_pct < -10%（明顯超過設定的停損門檻）

用法：
    python3 scripts/diag_sells.py            # 預設今日
    python3 scripts/diag_sells.py --days 3   # 近 3 天
    python3 scripts/diag_sells.py --all      # 全部
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = PROJECT_ROOT / "data" / "sector_accounts"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="近 N 天（含今日）")
    ap.add_argument("--all", action="store_true", help="不限日期")
    args = ap.parse_args()

    if args.all:
        start_date = None
    else:
        start_date = (date.today() - timedelta(days=args.days - 1)).isoformat()

    sectors = ["semiconductor", "electronics", "finance", "traditional",
               "precision", "other"]

    total_lag_loss = 0
    grand_total = 0

    for sector in sectors:
        path = ACCOUNTS_DIR / f"{sector}_account.json"
        if not path.exists():
            continue
        with open(path) as f:
            d = json.load(f)

        sells = [h for h in d.get("history", []) if h.get("type") == "SELL"]
        if start_date:
            sells = [h for h in sells if h.get("time", "") >= start_date]
        if not sells:
            continue

        print(f"=== {sector} (篩出 {len(sells)} 筆 SELL) ===")
        print(f"{'時間':<20}{'股票':<22}{'qty':<6}{'價':<10}{'損益':<10}{'%':<8}{'lag?':<6}{'訊號'}")
        print("-" * 120)

        sector_lag_loss = 0
        sector_total = 0
        for s in sorted(sells, key=lambda x: x.get("time", "")):
            t = s.get("time", "")[11:19]
            d_ = s.get("time", "")[:10]
            sym = s.get("symbol", "")
            name = s.get("name", "") or ""
            qty = s.get("qty", 0)
            price = s.get("price", 0)
            profit = s.get("profit", 0) or 0
            pct = s.get("profit_pct", 0) or 0
            signal = (s.get("signal") or "")[:50]

            # 顯示「中文名(代號)」格式，例：瑞昱(2379)
            code = sym.replace(".TW", "").replace(".TWO", "")
            sym_display = f"{name}({code})" if name and name != sym else sym

            # 判斷 lag：停損 / 趨勢破壞 + profit_pct < -10.5%
            is_stop_signal = ("停損" in signal or "趨勢破壞" in signal)
            is_lag = is_stop_signal and pct < -10.5
            lag_flag = "🔴 LAG" if is_lag else ""

            sector_total += profit
            if is_lag:
                sector_lag_loss += profit  # negative number

            print(f"{d_} {t:<9}{sym_display:<22}{qty:<6}{price:<10.2f}{profit:<10.0f}{pct:<8.2f}{lag_flag:<6}{signal}")

        grand_total += sector_total
        total_lag_loss += sector_lag_loss
        print(f"{'sector 總':<20}{'':<10}{'':<6}{'':<10}{sector_total:<10.0f}")
        if sector_lag_loss < 0:
            print(f"  其中 lag 出場虧損: {sector_lag_loss:.0f}")
        print()

    print("=" * 110)
    print(f"全部 sector 總損益: {grand_total:.0f}")
    if total_lag_loss < 0:
        non_lag = grand_total - total_lag_loss
        print(f"  lag 出場累計虧損:  {total_lag_loss:.0f} (這部分若連線正常本可避免)")
        print(f"  非 lag 部分損益:   {non_lag:.0f}")


if __name__ == "__main__":
    main()
