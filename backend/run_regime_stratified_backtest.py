"""
盤勢分層回測：買入訊號在不同 regime 下的後續報酬

測試問題：在「空頭」「盤整」「底部轉強」「多頭」「強勢多頭」「高檔轉折」等盤勢下，
buy_score >= 40 觸發後的 +5/+10/+20 日報酬與勝率分別如何？

如果某些盤勢下買入訊號是虧錢的，就應該加入 regime 過濾。
"""

import os
import sys
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from layers.regime import RegimeLayer
from screener import SCREENER_UNIVERSE, get_sector_weights
from signal_performance import _fetch_price_history, ANALYSIS_START

logging.basicConfig(level=logging.WARNING)

THRESHOLD = 40
HORIZONS = [5, 10, 20]
REGIMES = ["強勢多頭", "多頭", "底部轉強", "盤整", "高檔轉折", "空頭", "未知"]


def _backtest_single(symbol: str):
    df = _fetch_price_history(symbol)
    if df is None:
        return None

    sector_w = get_sector_weights(symbol)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    start_dt = pd.to_datetime(ANALYSIS_START)
    all_dates = list(df.index)

    agg = SignalAggregator(weights=sector_w)
    df_full = agg.calculate_all(df.copy())
    regime_layer = RegimeLayer(enabled=True)

    closes = df_full['close'].values
    prev_buy = False
    events = []

    for i, dt in enumerate(all_dates):
        if dt < start_dt or i < 120:
            continue
        sub_df = df_full.iloc[:i + 1]
        try:
            sig = agg.generate_signals(sub_df, symbol, "1d")
            buy_score = sig.buy_score
        except Exception:
            buy_score = 0
        try:
            mod = regime_layer.compute_modifier(symbol, sub_df)
            regime_state = mod.regime or "未知"
        except Exception:
            regime_state = "未知"

        is_buy = buy_score >= THRESHOLD
        if is_buy and not prev_buy:
            forward = {}
            for h in HORIZONS:
                if i + h < len(closes):
                    forward[h] = (closes[i + h] / closes[i] - 1) * 100
                else:
                    forward[h] = None
            events.append({"regime": regime_state, "forward": forward})
        prev_buy = is_buy

    return events


def main():
    print(f"\n回測：buy_score >= {THRESHOLD} 觸發後依 regime 分層報酬")
    print(f"期間: {ANALYSIS_START} ~ today\n")

    universe = dict(SCREENER_UNIVERSE)
    all_events = []
    completed = 0

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_backtest_single, s): s for s in universe}
        for fut in as_completed(futs):
            completed += 1
            try:
                res = fut.result(timeout=60)
                if res:
                    all_events.extend(res)
            except Exception:
                pass
            if completed % 20 == 0:
                print(f"  進度 {completed}/{len(universe)}")

    print("\n" + "=" * 100)
    print(f"買入訊號（buy_score >= {THRESHOLD}）依進場時 regime 分層")
    print("=" * 100)
    print(f"{'盤勢':>10s} {'觸發數':>6s} {'佔比':>6s} | "
          + " | ".join(f"+{h:>2d}d 平均/勝率/中位".rjust(22) for h in HORIZONS))
    print("-" * 100)

    by_regime = defaultdict(list)
    for e in all_events:
        by_regime[e["regime"]].append(e)

    total = len(all_events)
    for r in REGIMES:
        events = by_regime.get(r, [])
        n = len(events)
        if n == 0:
            continue
        pct = n / total * 100
        row = [f"{r:>10s}", f"{n:>6d}", f"{pct:>5.1f}%"]
        for h in HORIZONS:
            arr = [e["forward"][h] for e in events if e["forward"][h] is not None]
            if not arr:
                row.append("無樣本".rjust(22))
                continue
            avg = sum(arr) / len(arr)
            wins = sum(1 for x in arr if x > 0) / len(arr) * 100
            med = sorted(arr)[len(arr) // 2]
            row.append(f"{avg:+6.2f}% / {wins:5.1f}% / {med:+5.2f}%".rjust(22))
        print(" ".join(row))

    print("\n" + "=" * 100)
    print("結論建議：")
    print("=" * 100)
    for r in REGIMES:
        events = by_regime.get(r, [])
        if not events:
            continue
        h = 10
        arr = [e["forward"][h] for e in events if e["forward"][h] is not None]
        if not arr:
            continue
        avg = sum(arr) / len(arr)
        wins = sum(1 for x in arr if x > 0) / len(arr) * 100
        if avg > 2.0 and wins > 55:
            verdict = "✅ 強訊號 — 應保留並可加碼"
        elif avg > 0.5 and wins > 50:
            verdict = "🟡 普通 — 保留但減碼"
        elif avg < -0.5 or wins < 45:
            verdict = "❌ 虧錢 — 建議過濾"
        else:
            verdict = "⚪ 持平 — 可保留"
        print(f"  {r:>10s} (+10d): 平均 {avg:+5.2f}%  勝率 {wins:5.1f}%  → {verdict}")


if __name__ == "__main__":
    main()
