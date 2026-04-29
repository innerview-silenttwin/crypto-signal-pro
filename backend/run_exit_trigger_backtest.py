"""
出場觸發條件回測（Exit Trigger Backtest）

問題：現有 RegimeLayer 對「已在中段位置開始破線」反應遲鈍，
導致股票跌破上升趨勢但仍判定為 BULL，sell_multiplier 縮 30%
讓賣訊達不到門檻。

本回測測試一組價格/量能型出場/進場觸發，找出最適合補強的條件。

評估流程（事件研究法）：
1. 對每檔股票每日，判斷各候選觸發是否成立
2. 紀錄當日 regime、+5d/+10d/+20d 後續報酬
3. 對「賣早事件」（賣後 +20d 反彈 ≥ 5%）做再進場分析：
   - A. 系統有買回（用簡化 buy proxy 判斷）
   - B. 沒買回但反彈 → 完全踏空
   - C. 真的續跌 → 賣對了
4. 輸出每觸發 × 每 regime 的指標：勝率、淨經濟效益、誤判率

輸出：
- backtest_results/exit_triggers_{ts}.csv
- backtest_results/bottom_triggers_{ts}.csv
- backtest_results/exit_triggers_summary_{ts}.md
"""

import os
import sys
import json
import logging
from datetime import datetime
from typing import Optional, Dict, List
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from screener import SCREENER_UNIVERSE, get_symbol_sector, get_sector_weights
from signals.aggregator import SignalAggregator
from layers.regime import RegimeLayer
from signal_performance import _fetch_price_history

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ── 設定 ─────────────────────────────────────────────────────────

ANALYSIS_START = "2024-01-01"
HORIZONS = [5, 10, 20]
REBOUND_THRESHOLD_PCT = 5.0    # +20d 反彈超過此 % 視為「賣早」
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "backtest_results")
os.makedirs(RESULTS_DIR, exist_ok=True)


# ── 候選觸發定義 ─────────────────────────────────────────────────

def _consec_down_pct(closes: np.ndarray, i: int, n: int) -> float:
    """近 n 日累計報酬率 %（負值代表跌）"""
    if i < n:
        return 0.0
    return (closes[i] / closes[i - n] - 1) * 100


def _all_red(opens: np.ndarray, closes: np.ndarray, i: int, n: int) -> bool:
    """連續 n 日收盤 < 開盤（連黑）"""
    if i < n - 1:
        return False
    return all(closes[i - k] < opens[i - k] for k in range(n))


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
         i: int, period: int = 14) -> float:
    """簡化 ATR，回傳當下值"""
    if i < period:
        return 0.0
    tr_list = []
    for k in range(period):
        h = highs[i - k]
        l = lows[i - k]
        pc = closes[i - k - 1] if i - k - 1 >= 0 else closes[i - k]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        tr_list.append(tr)
    return float(np.mean(tr_list))


def evaluate_sell_triggers(
    df: pd.DataFrame, ma20: np.ndarray, ma60: np.ndarray,
    vol_ma20: np.ndarray, atr14: np.ndarray, i: int
) -> Dict[str, bool]:
    """回傳該日各候選賣出觸發是否成立"""
    closes = df['close'].values
    opens = df['open'].values
    highs = df['high'].values
    lows = df['low'].values
    vols = df['volume'].values

    if i < 60:  # 需 60 日歷史
        return {k: False for k in [
            'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8', 'S9'
        ]}

    c = closes[i]
    vol_ratio = vols[i] / vol_ma20[i] if vol_ma20[i] > 0 else 0
    daily_chg_pct = (closes[i] / closes[i - 1] - 1) * 100 if i > 0 else 0
    d2 = _consec_down_pct(closes, i, 2)
    d3 = _consec_down_pct(closes, i, 3)
    ma20_break = (c / ma20[i] - 1) * 100 if ma20[i] > 0 else 0  # 負=跌破
    high_20d = float(np.max(highs[i - 19:i + 1])) if i >= 19 else c

    return {
        'S1': d2 <= -3.0,
        'S2': d2 <= -5.0,
        'S3': d3 <= -5.0,
        'S4': d3 <= -7.0,
        'S5': ma20_break <= -2.0,
        'S6': ma20_break <= -2.0 and vol_ratio > 1.5,
        'S7': daily_chg_pct <= -4.0 and vol_ratio > 2.0,
        'S8': (atr14[i] > 0 and (high_20d - c) >= 3.0 * atr14[i]),
        'S9': _all_red(opens, closes, i, 3) and c < ma20[i] * 1.0,  # 連3黑+收盤<20MA
    }


def evaluate_buy_triggers(
    df: pd.DataFrame, ma20: np.ndarray, ma60: np.ndarray,
    vol_ma20: np.ndarray, atr14: np.ndarray, i: int
) -> Dict[str, bool]:
    """回傳該日各候選底部觸發是否成立"""
    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    vols = df['volume'].values

    if i < 60:
        return {k: False for k in ['B1', 'B2', 'B3', 'B4', 'B5', 'B6']}

    c = closes[i]
    vol_ratio = vols[i] / vol_ma20[i] if vol_ma20[i] > 0 else 0
    daily_chg_pct = (closes[i] / closes[i - 1] - 1) * 100 if i > 0 else 0
    u2 = _consec_down_pct(closes, i, 2)  # 同函數，正值代表漲
    u3 = _consec_down_pct(closes, i, 3)
    ma20_break = (c / ma20[i] - 1) * 100 if ma20[i] > 0 else 0  # 正=站上
    low_20d = float(np.min(lows[i - 19:i + 1])) if i >= 19 else c

    return {
        'B1': u2 >= 3.0,
        'B2': u3 >= 5.0,
        'B3': ma20_break >= 2.0,
        'B4': ma20_break >= 2.0 and vol_ratio > 1.5,
        'B5': daily_chg_pct >= 4.0 and vol_ratio > 2.0,
        'B6': (atr14[i] > 0 and (c - low_20d) >= 3.0 * atr14[i]),
    }


# ── 簡化版 buy proxy（用於再進場判斷）─────────────────────────────
# 不重算完整 SignalAggregator（成本高），用價量代理：
# 收盤 > 20MA AND 收盤 > 60MA AND 3 日動能正向

def buy_proxy_active(closes: np.ndarray, ma20: np.ndarray, ma60: np.ndarray,
                     i: int) -> bool:
    """簡化 buy 信號 proxy"""
    if i < 60 or i < 3:
        return False
    if ma20[i] <= 0 or ma60[i] <= 0:
        return False
    return (closes[i] > ma20[i]
            and closes[i] > ma60[i]
            and (closes[i] / closes[i - 3] - 1) > 0.01)


# ── 單檔回測 ─────────────────────────────────────────────────────

def backtest_one(symbol: str) -> Optional[dict]:
    df = _fetch_price_history(symbol, ANALYSIS_START)
    if df is None or len(df) < 100:
        return None

    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    start_dt = pd.to_datetime(ANALYSIS_START)

    # 預算指標（一次）
    sector_w = get_sector_weights(symbol)
    sector_id = get_symbol_sector(symbol)
    agg = SignalAggregator(weights=sector_w)
    df_full = agg.calculate_all(df.copy())

    closes = df_full['close'].values
    opens = df_full['open'].values
    highs = df_full['high'].values
    lows = df_full['low'].values
    vols = df_full['volume'].values
    n = len(df_full)

    # 預算 MA 與 ATR
    ma20 = pd.Series(closes).rolling(20).mean().fillna(0).values
    ma60 = pd.Series(closes).rolling(60).mean().fillna(0).values
    vol_ma20 = pd.Series(vols).rolling(20).mean().fillna(1).values
    atr14 = np.zeros(n)
    for i in range(14, n):
        atr14[i] = _atr(highs, lows, closes, i, 14)

    regime_layer = RegimeLayer(enabled=True)

    sell_events = []
    buy_events = []

    for i in range(60, n - 21):  # 預留 20 日 lookahead
        d = df_full.index[i]
        if d < start_dt:
            continue

        c = closes[i]

        # --- 取 regime（成本最高，但每日只一次）---
        try:
            mod = regime_layer.compute_modifier(symbol, df_full.iloc[:i + 1],
                                                sector_id)
            regime = mod.regime or "盤整"
        except Exception:
            regime = "盤整"

        # --- 後續報酬 ---
        forwards = {}
        for h in HORIZONS:
            if i + h >= n:
                forwards[f'fwd_{h}d'] = None
            else:
                forwards[f'fwd_{h}d'] = (closes[i + h] / c - 1) * 100

        # --- Sell triggers ---
        sells = evaluate_sell_triggers(df_full, ma20, ma60, vol_ma20, atr14, i)
        for tag, fired in sells.items():
            if not fired:
                continue
            ev = {
                'symbol': symbol, 'date': d.strftime("%Y-%m-%d"),
                'trigger': tag, 'regime': regime, 'price': float(c),
                **forwards,
            }
            # 賣早分析：+20d 反彈 ≥ 5% 即視為候選「賣早」
            fwd_20 = forwards.get('fwd_20d')
            if fwd_20 is not None and fwd_20 >= REBOUND_THRESHOLD_PCT:
                # 找 T+1 ~ T+20 之間 buy_proxy_active 的第一日
                rebuy_idx = None
                for j in range(i + 1, min(i + 21, n)):
                    if buy_proxy_active(closes, ma20, ma60, j):
                        rebuy_idx = j
                        break
                if rebuy_idx is not None:
                    rebuy_price = closes[rebuy_idx]
                    final_price = closes[i + 20]
                    ev['outcome'] = 'A_rebuy'
                    ev['rebuy_days'] = rebuy_idx - i
                    ev['rebuy_cost_pct'] = (rebuy_price / c - 1) * 100  # 買回成本（負=低於賣價）
                    ev['captured_pct'] = (final_price / rebuy_price - 1) * 100
                else:
                    ev['outcome'] = 'B_full_miss'
                    ev['rebuy_days'] = None
                    ev['rebuy_cost_pct'] = None
                    ev['captured_pct'] = None
            elif fwd_20 is not None and fwd_20 < REBOUND_THRESHOLD_PCT:
                ev['outcome'] = 'C_correct_sell'
                ev['rebuy_days'] = None
                ev['rebuy_cost_pct'] = None
                ev['captured_pct'] = None
            else:
                ev['outcome'] = None
            sell_events.append(ev)

        # --- Buy triggers ---
        buys = evaluate_buy_triggers(df_full, ma20, ma60, vol_ma20, atr14, i)
        for tag, fired in buys.items():
            if not fired:
                continue
            buy_events.append({
                'symbol': symbol, 'date': d.strftime("%Y-%m-%d"),
                'trigger': tag, 'regime': regime, 'price': float(c),
                **forwards,
            })

    return {'symbol': symbol, 'sell_events': sell_events, 'buy_events': buy_events}


# ── 全流程 ───────────────────────────────────────────────────────

def run_all(max_workers: int = 6) -> tuple:
    symbols = list(SCREENER_UNIVERSE.keys())
    print(f"開始回測 {len(symbols)} 檔股票（{ANALYSIS_START} ~ now）")
    print(f"Workers: {max_workers}")

    all_sell = []
    all_buy = []
    done = 0
    failed = 0

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(backtest_one, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                r = fut.result()
                if r is None:
                    failed += 1
                    print(f"  [SKIP] {sym}")
                else:
                    all_sell.extend(r['sell_events'])
                    all_buy.extend(r['buy_events'])
                done += 1
                if done % 5 == 0:
                    print(f"  進度 {done}/{len(symbols)} (sell={len(all_sell)}, buy={len(all_buy)})")
            except Exception as e:
                failed += 1
                print(f"  [ERR] {sym}: {e}")

    print(f"完成：成功 {done - failed}/{len(symbols)}，失敗 {failed}")
    return pd.DataFrame(all_sell), pd.DataFrame(all_buy)


# ── 摘要報告 ─────────────────────────────────────────────────────

def summarize_sells(df: pd.DataFrame) -> pd.DataFrame:
    """每觸發 × regime × horizon 的勝率/平均報酬"""
    rows = []
    for trig in sorted(df['trigger'].unique()):
        for regime in [None, '強勢多頭', '多頭', '盤整', '空頭', '高檔轉折', '底部轉強']:
            sub = df[df['trigger'] == trig]
            if regime is not None:
                sub = sub[sub['regime'] == regime]
            if len(sub) < 5:
                continue
            row = {
                'trigger': trig,
                'regime': regime or 'ALL',
                'n': len(sub),
            }
            for h in HORIZONS:
                col = f'fwd_{h}d'
                vals = sub[col].dropna()
                if len(vals) == 0:
                    continue
                row[f'avg_{h}d'] = round(vals.mean(), 2)
                row[f'win_{h}d'] = round((vals < 0).sum() / len(vals) * 100, 1)
                row[f'p25_{h}d'] = round(vals.quantile(0.25), 2)
                row[f'p75_{h}d'] = round(vals.quantile(0.75), 2)
            # 賣飛分析（僅當 regime=ALL 才算，避免重複）
            if regime is None:
                outcomes = sub['outcome'].value_counts(dropna=False)
                total = len(sub.dropna(subset=['outcome']))
                if total > 0:
                    row['correct_sell_pct'] = round(
                        outcomes.get('C_correct_sell', 0) / total * 100, 1)
                    row['rebuy_pct'] = round(
                        outcomes.get('A_rebuy', 0) / total * 100, 1)
                    row['full_miss_pct'] = round(
                        outcomes.get('B_full_miss', 0) / total * 100, 1)
                # 平均買回成本
                a_subset = sub[sub['outcome'] == 'A_rebuy']
                if len(a_subset) > 0:
                    row['avg_rebuy_cost_pct'] = round(
                        a_subset['rebuy_cost_pct'].mean(), 2)
                    row['avg_rebuy_days'] = round(
                        a_subset['rebuy_days'].mean(), 1)
            rows.append(row)
    return pd.DataFrame(rows)


def summarize_buys(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for trig in sorted(df['trigger'].unique()):
        for regime in [None, '強勢多頭', '多頭', '盤整', '空頭', '高檔轉折', '底部轉強']:
            sub = df[df['trigger'] == trig]
            if regime is not None:
                sub = sub[sub['regime'] == regime]
            if len(sub) < 5:
                continue
            row = {
                'trigger': trig,
                'regime': regime or 'ALL',
                'n': len(sub),
            }
            for h in HORIZONS:
                col = f'fwd_{h}d'
                vals = sub[col].dropna()
                if len(vals) == 0:
                    continue
                row[f'avg_{h}d'] = round(vals.mean(), 2)
                row[f'win_{h}d'] = round((vals > 0).sum() / len(vals) * 100, 1)
                row[f'p25_{h}d'] = round(vals.quantile(0.25), 2)
                row[f'p75_{h}d'] = round(vals.quantile(0.75), 2)
            rows.append(row)
    return pd.DataFrame(rows)


def write_report(sells: pd.DataFrame, buys: pd.DataFrame,
                 sells_summ: pd.DataFrame, buys_summ: pd.DataFrame,
                 out_path: str):
    lines = []
    lines.append("# 出場/進場觸發回測報告")
    lines.append(f"\n生成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"分析期間：{ANALYSIS_START} ~ now")
    lines.append(f"樣本：{sells['symbol'].nunique() if len(sells) else 0} 檔股票")
    lines.append(f"事件：賣出觸發 {len(sells)} 筆、買入觸發 {len(buys)} 筆")
    lines.append(f"賣早門檻：+20d 反彈 ≥ {REBOUND_THRESHOLD_PCT}%")

    lines.append("\n## 觸發定義")
    lines.append("""
| 編號 | 條件 |
|---|---|
| S1 | 連跌 2 日累計 ≥ 3% |
| S2 | 連跌 2 日累計 ≥ 5% |
| S3 | 連跌 3 日累計 ≥ 5% |
| S4 | 連跌 3 日累計 ≥ 7% |
| S5 | 跌破 20MA ≥ 2% |
| S6 | 跌破 20MA ≥ 2% + 量比 > 1.5 |
| S7 | 單日跌 ≥ 4% + 量比 > 2 |
| S8 | 從近 20 日高點下跌 ≥ 3×ATR |
| S9 | 連續 3 黑 K + 收盤 < 20MA |
| B1 | 連漲 2 日累計 ≥ 3% |
| B2 | 連漲 3 日累計 ≥ 5% |
| B3 | 站上 20MA ≥ 2% |
| B4 | 站上 20MA ≥ 2% + 量比 > 1.5 |
| B5 | 單日漲 ≥ 4% + 量比 > 2 |
| B6 | 從近 20 日低點反彈 ≥ 3×ATR |
""")

    lines.append("\n## 賣出觸發（ALL regime 摘要）\n")
    sells_all = sells_summ[sells_summ['regime'] == 'ALL'].sort_values('avg_10d')
    if len(sells_all) > 0:
        lines.append("| 觸發 | n | +5d 平均 | +5d 勝率 | +10d 平均 | +10d 勝率 | +20d 平均 | 賣對% | 買回% | 全踏空% |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for _, r in sells_all.iterrows():
            lines.append(
                f"| {r['trigger']} | {r['n']} | {r.get('avg_5d','-')}% | {r.get('win_5d','-')}% "
                f"| {r.get('avg_10d','-')}% | {r.get('win_10d','-')}% "
                f"| {r.get('avg_20d','-')}% "
                f"| {r.get('correct_sell_pct','-')}% | {r.get('rebuy_pct','-')}% "
                f"| {r.get('full_miss_pct','-')}% |"
            )

    lines.append("\n### 解讀指引（賣出）\n")
    lines.append("- **+10d 平均負值越大** → 觸發後越能避過跌幅")
    lines.append("- **賣對% 高、全踏空% 低** → 系統不會因為賣早而踏空")
    lines.append("- **買回% 高+買回成本接近 0** → 賣早也能修正回來，淨損失小")
    lines.append("- **理想觸發**：+10d 平均 ≤ -2%、賣對率 ≥ 60%、全踏空率 ≤ 15%")

    lines.append("\n## 賣出觸發（依 regime 分層）\n")
    for regime in ['多頭', '強勢多頭', '盤整', '高檔轉折']:
        sub = sells_summ[sells_summ['regime'] == regime].sort_values('avg_10d')
        if len(sub) == 0:
            continue
        lines.append(f"\n### regime = {regime}\n")
        lines.append("| 觸發 | n | +5d | +10d | +20d | +10d 勝率 |")
        lines.append("|---|---|---|---|---|---|")
        for _, r in sub.iterrows():
            lines.append(
                f"| {r['trigger']} | {r['n']} | {r.get('avg_5d','-')}% "
                f"| {r.get('avg_10d','-')}% | {r.get('avg_20d','-')}% "
                f"| {r.get('win_10d','-')}% |"
            )

    lines.append("\n## 買入觸發（ALL regime 摘要）\n")
    buys_all = buys_summ[buys_summ['regime'] == 'ALL'].sort_values('avg_10d', ascending=False)
    if len(buys_all) > 0:
        lines.append("| 觸發 | n | +5d 平均 | +5d 勝率 | +10d 平均 | +10d 勝率 | +20d 平均 |")
        lines.append("|---|---|---|---|---|---|---|")
        for _, r in buys_all.iterrows():
            lines.append(
                f"| {r['trigger']} | {r['n']} | {r.get('avg_5d','-')}% | {r.get('win_5d','-')}% "
                f"| {r.get('avg_10d','-')}% | {r.get('win_10d','-')}% "
                f"| {r.get('avg_20d','-')}% |"
            )

    lines.append("\n## 買入觸發（依 regime 分層）\n")
    for regime in ['空頭', '盤整', '底部轉強', '多頭']:
        sub = buys_summ[buys_summ['regime'] == regime].sort_values('avg_10d', ascending=False)
        if len(sub) == 0:
            continue
        lines.append(f"\n### regime = {regime}\n")
        lines.append("| 觸發 | n | +5d | +10d | +20d | +10d 勝率 |")
        lines.append("|---|---|---|---|---|---|")
        for _, r in sub.iterrows():
            lines.append(
                f"| {r['trigger']} | {r['n']} | {r.get('avg_5d','-')}% "
                f"| {r.get('avg_10d','-')}% | {r.get('avg_20d','-')}% "
                f"| {r.get('win_10d','-')}% |"
            )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    sells, buys = run_all(max_workers=6)

    if len(sells) == 0:
        print("❌ 沒有任何賣出事件")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 原始事件 csv
    sells_csv = os.path.join(RESULTS_DIR, f"exit_triggers_events_{ts}.csv")
    buys_csv = os.path.join(RESULTS_DIR, f"bottom_triggers_events_{ts}.csv")
    sells.to_csv(sells_csv, index=False, encoding='utf-8-sig')
    buys.to_csv(buys_csv, index=False, encoding='utf-8-sig')

    # 摘要 csv
    sells_summ = summarize_sells(sells)
    buys_summ = summarize_buys(buys)
    sells_summ_csv = os.path.join(RESULTS_DIR, f"exit_triggers_summary_{ts}.csv")
    buys_summ_csv = os.path.join(RESULTS_DIR, f"bottom_triggers_summary_{ts}.csv")
    sells_summ.to_csv(sells_summ_csv, index=False, encoding='utf-8-sig')
    buys_summ.to_csv(buys_summ_csv, index=False, encoding='utf-8-sig')

    # 報告 md
    report_path = os.path.join(RESULTS_DIR, f"exit_triggers_report_{ts}.md")
    write_report(sells, buys, sells_summ, buys_summ, report_path)

    print(f"\n✅ 完成")
    print(f"事件：{sells_csv}")
    print(f"     {buys_csv}")
    print(f"摘要：{sells_summ_csv}")
    print(f"     {buys_summ_csv}")
    print(f"報告：{report_path}")
    print(f"\n=== 賣出觸發（ALL）摘要前10 ===")
    print(sells_summ[sells_summ['regime'] == 'ALL'].sort_values('avg_10d').head(10))
    print(f"\n=== 買入觸發（ALL）摘要前10 ===")
    print(buys_summ[buys_summ['regime'] == 'ALL'].sort_values('avg_10d', ascending=False).head(10))


if __name__ == "__main__":
    main()
