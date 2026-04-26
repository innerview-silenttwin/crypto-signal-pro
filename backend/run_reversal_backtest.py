"""
反轉訊號交叉回測：底部轉強 / 高檔轉折 與 籌碼/技術 的最佳組合

問題：底部轉強與高檔轉折都是「趨勢反轉」訊號，但孤立使用效果普通
（底部轉強 +10d 平均 +2.44%、高檔轉折 +10d 平均 +12.86%）。

本回測對每個反轉事件，分層：
1. 是否同時 buy_score >= 40
2. 是否同時 投信連買 >= 3
3. 是否同時 外資連買 >= 3
4. 是否同時 60MA 上揚

找出哪些「反轉 + 確認」組合是黃金訊號，哪些是地雷。
"""

import os
import sys
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from layers.regime import RegimeLayer
from screener import SCREENER_UNIVERSE, get_sector_weights
from signal_performance import (
    _fetch_price_history, _get_institutional_data, _compute_chip_day,
    ANALYSIS_START,
)

logging.basicConfig(level=logging.WARNING)

HORIZONS = [5, 10, 20]


def _backtest_single(symbol: str):
    df = _fetch_price_history(symbol)
    if df is None:
        return None

    inst_data = _get_institutional_data(symbol)
    sector_w = get_sector_weights(symbol)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    start_dt = pd.to_datetime(ANALYSIS_START)
    all_dates = list(df.index)

    agg = SignalAggregator(weights=sector_w)
    df_full = agg.calculate_all(df.copy())
    regime_layer = RegimeLayer(enabled=True)

    closes = df_full['close'].values

    prev_bottom = False
    prev_top = False
    bottom_events = []
    top_events = []

    for i, dt in enumerate(all_dates):
        if dt < start_dt or i < 120:
            continue
        sub_df = df_full.iloc[:i + 1]
        date_str = dt.strftime("%Y-%m-%d")

        try:
            sig = agg.generate_signals(sub_df, symbol, "1d")
            buy_score = sig.buy_score
        except Exception:
            buy_score = 0

        try:
            mod = regime_layer.compute_modifier(symbol, sub_df)
            regime_state = mod.regime or "未知"
            details = mod.details or {}
            ma60_up = details.get("ma_alignment", {}).get("ma60_up", False)
        except Exception:
            regime_state = "未知"
            ma60_up = False

        chip = _compute_chip_day(inst_data, date_str)
        foreign = chip["foreign_consec_buy"] >= 3
        trust = chip["trust_consec_buy"] >= 3

        # 計算未來報酬
        forward = {}
        for h in HORIZONS:
            if i + h < len(closes):
                forward[h] = (closes[i + h] / closes[i] - 1) * 100
            else:
                forward[h] = None

        is_bottom = regime_state == "底部轉強"
        if is_bottom and not prev_bottom:
            bottom_events.append({
                "forward": forward,
                "buy_score_high": buy_score >= 40,
                "trust": trust,
                "foreign": foreign,
                "ma60_up": ma60_up,
            })
        prev_bottom = is_bottom

        is_top = regime_state == "高檔轉折"
        if is_top and not prev_top:
            top_events.append({
                "forward": forward,
                "buy_score_high": buy_score >= 40,
                "trust": trust,
                "foreign": foreign,
                "ma60_up": ma60_up,
            })
        prev_top = is_top

    return {"bottom": bottom_events, "top": top_events}


def stat_block(events, label):
    n = len(events)
    if n == 0:
        return f"{label}: 無樣本"
    arr_5 = [e["forward"][5] for e in events if e["forward"][5] is not None]
    arr_10 = [e["forward"][10] for e in events if e["forward"][10] is not None]
    arr_20 = [e["forward"][20] for e in events if e["forward"][20] is not None]
    line = f"{label:>40s}: N={n:>3d} "
    for h, arr in [(5, arr_5), (10, arr_10), (20, arr_20)]:
        if not arr:
            line += f" +{h}d:無 "
        else:
            avg = sum(arr) / len(arr)
            wins = sum(1 for x in arr if x > 0) / len(arr) * 100
            line += f" +{h}d:{avg:+5.2f}%/{wins:4.1f}%"
    return line


def analyze_section(events, regime_label):
    print(f"\n{'='*100}")
    print(f"【{regime_label}】交叉分層分析")
    print('='*100)

    print(stat_block(events, f"全部 {regime_label}"))
    print(f"\n--- 單一條件分層 ---")
    print(stat_block([e for e in events if e["buy_score_high"]], f"  + buy_score≥40"))
    print(stat_block([e for e in events if not e["buy_score_high"]], f"  + buy_score<40"))
    print(stat_block([e for e in events if e["trust"]], f"  + 投信連買≥3"))
    print(stat_block([e for e in events if not e["trust"]], f"  + 投信無連買"))
    print(stat_block([e for e in events if e["foreign"]], f"  + 外資連買≥3"))
    print(stat_block([e for e in events if not e["foreign"]], f"  + 外資無連買"))
    print(stat_block([e for e in events if e["ma60_up"]], f"  + 60MA上揚"))
    print(stat_block([e for e in events if not e["ma60_up"]], f"  + 60MA下彎"))

    print(f"\n--- 雙條件交集 ---")
    print(stat_block([e for e in events if e["trust"] and e["foreign"]], f"  + 投信+外資雙連買"))
    print(stat_block([e for e in events if e["trust"] and e["buy_score_high"]], f"  + 投信連買 & buy≥40"))
    print(stat_block([e for e in events if e["trust"] and e["ma60_up"]], f"  + 投信連買 & 60MA上揚"))
    print(stat_block([e for e in events if e["buy_score_high"] and e["ma60_up"]], f"  + buy≥40 & 60MA上揚"))

    print(f"\n--- 三條件黃金組合 ---")
    print(stat_block([e for e in events if e["trust"] and e["foreign"] and e["ma60_up"]],
                     f"  + 雙法人連買 & 60MA上揚"))
    print(stat_block([e for e in events if e["trust"] and e["buy_score_high"] and e["ma60_up"]],
                     f"  + 投信 & buy≥40 & 60MA上揚"))
    print(stat_block([e for e in events if e["trust"] and e["foreign"] and e["buy_score_high"]],
                     f"  + 雙法人 & buy≥40"))


def main():
    print(f"\n反轉訊號交叉回測  期間: {ANALYSIS_START} ~ today\n")
    universe = dict(SCREENER_UNIVERSE)
    all_bottom = []
    all_top = []
    completed = 0

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(_backtest_single, s): s for s in universe}
        for fut in as_completed(futs):
            completed += 1
            try:
                res = fut.result(timeout=60)
                if res:
                    all_bottom.extend(res["bottom"])
                    all_top.extend(res["top"])
            except Exception:
                pass
            if completed % 20 == 0:
                print(f"  進度 {completed}/{len(universe)}")

    analyze_section(all_bottom, "底部轉強")
    analyze_section(all_top, "高檔轉折")


if __name__ == "__main__":
    main()
