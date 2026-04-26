"""
門檻回測：評估 buy_score 不同觸發門檻的勝率與報酬

對所有股票逐日計算 buy_score，並對 [35, 40, 45, 50, 55, 60, 65] 多個門檻
分別記錄「邊緣觸發」事件，計算 +5/+10/+20 交易日的勝率與平均報酬。

同時評估底部轉強信號修改前後的差異。
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from layers.regime import RegimeLayer
from screener import SCREENER_UNIVERSE, get_sector_weights
from signal_performance import _fetch_price_history, ANALYSIS_START

logging.basicConfig(level=logging.WARNING)

THRESHOLDS = [35, 40, 45, 50, 55, 60, 65]
HORIZONS = [5, 10, 20]


def _backtest_single_stock(symbol: str):
    """回跑單一股票，回傳每個門檻的觸發事件清單（含未來報酬）"""
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

    # 每個門檻的 prev 狀態
    prev_buy = {t: False for t in THRESHOLDS}
    triggers = {t: [] for t in THRESHOLDS}

    # 底部轉強事件
    prev_bottom = False
    bottom_events = []

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
            regime_state = mod.regime or ""
        except Exception:
            regime_state = ""

        # 每個門檻觸發追蹤
        for t in THRESHOLDS:
            is_buy = buy_score >= t
            if is_buy and not prev_buy[t]:
                # 計算未來報酬
                forward = {}
                for h in HORIZONS:
                    if i + h < len(closes):
                        forward[h] = (closes[i + h] / closes[i] - 1) * 100
                    else:
                        forward[h] = None
                triggers[t].append({"forward": forward, "score": buy_score})
            prev_buy[t] = is_buy

        # 底部轉強事件追蹤
        is_bottom = regime_state == "底部轉強"
        if is_bottom and not prev_bottom:
            forward = {}
            for h in HORIZONS:
                if i + h < len(closes):
                    forward[h] = (closes[i + h] / closes[i] - 1) * 100
                else:
                    forward[h] = None
            bottom_events.append({"forward": forward})
        prev_bottom = is_bottom

    return {"triggers": triggers, "bottom": bottom_events}


def main():
    print(f"\n回測期間: {ANALYSIS_START} ~ today")
    print(f"門檻: {THRESHOLDS}")
    print(f"前向視窗: {HORIZONS} 交易日\n")

    universe = dict(SCREENER_UNIVERSE)
    all_triggers = {t: [] for t in THRESHOLDS}
    all_bottom = []

    completed = 0
    total = len(universe)
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_backtest_single_stock, s): s for s in universe}
        for fut in as_completed(futs):
            completed += 1
            try:
                res = fut.result(timeout=60)
                if res:
                    for t in THRESHOLDS:
                        all_triggers[t].extend(res["triggers"][t])
                    all_bottom.extend(res["bottom"])
            except Exception:
                pass
            if completed % 20 == 0:
                print(f"  進度 {completed}/{total}")

    print("\n" + "=" * 90)
    print("【方案 B】buy_score 多門檻回測：哪個門檻最有效？")
    print("=" * 90)
    print(f"{'門檻':>6s} {'總觸發':>8s} | "
          + " | ".join(f"+{h:>2d}d 平均/勝率/中位".rjust(22) for h in HORIZONS))
    print("-" * 90)

    for t in THRESHOLDS:
        events = all_triggers[t]
        n = len(events)
        row = [f"{t:>6d}", f"{n:>8d}"]
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

    print("\n" + "=" * 90)
    print("【底部轉強】當前邏輯下的觸發效果（修改前基線）")
    print("=" * 90)
    print(f"  總觸發數: {len(all_bottom)}")
    for h in HORIZONS:
        arr = [e["forward"][h] for e in all_bottom if e["forward"][h] is not None]
        if arr:
            avg = sum(arr) / len(arr)
            wins = sum(1 for x in arr if x > 0) / len(arr) * 100
            med = sorted(arr)[len(arr) // 2]
            print(f"  +{h}d: 平均 {avg:+.2f}%  勝率 {wins:.1f}%  中位 {med:+.2f}%")

    # 寫入結果以利後續對比
    out = {
        "thresholds": {
            str(t): [
                {h: e["forward"][h] for h in HORIZONS}
                for e in all_triggers[t]
            ]
            for t in THRESHOLDS
        },
        "bottom": [
            {h: e["forward"][h] for h in HORIZONS}
            for e in all_bottom
        ],
    }
    out_path = os.path.join(os.path.dirname(__file__), "data", "threshold_backtest.json")
    with open(out_path, "w") as f:
        json.dump(out, f)
    print(f"\n結果已寫入: {out_path}")


if __name__ == "__main__":
    main()
