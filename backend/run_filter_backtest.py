"""反假突破 / 假跌破 filter 回測

對 18 檔代表股 × 6 變體 × 5.5 年（2021-01 ~ 2026-06-08），
看哪個 filter 對哪檔股報酬最好 / MDD 最小。

3 個 filter（全當日判斷）：
  A 量能：    當日 vol_ratio >= 1.5 才買
  B 收盤確認：當日收盤 > 前 5 日 high × 1.005 + 收當日上半段 + 量配合
  C K 棒形態：紅 K 且實體佔比 >= 60%

註：B 原想用「13:00 後」但 daily backtest 不能模擬盤中時點，
   近似為「當日收盤站穩突破點」— 邏輯接近。

資料源：yfinance 直抓（dev 機）。
不動 production code、不動 sector_auto_trader。

執行：
  cd backend && python run_filter_backtest.py

輸出：
  backtest_results/filter_backtest_{ts}.csv
  backtest_results/filter_backtest_{ts}.md
"""

from __future__ import annotations

import os
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator, MarketType
from layers.regime import RegimeLayer


# ── 設定 ─────────────────────────────────────────────────────────

UNIVERSE = {
    "2330.TW": "台積電",  "2454.TW": "聯發科",  "2317.TW": "鴻海",
    "2357.TW": "華碩",    "2382.TW": "廣達",    "2383.TW": "台光電",
    "2882.TW": "國泰金",  "2881.TW": "富邦金",  "2891.TW": "中信金",
    "1101.TW": "台泥",    "1301.TW": "台塑",    "1326.TW": "台化",
    "7769.TW": "豐祥-KY", "2049.TW": "上銀",    "1590.TW": "亞德客-KY",
    "0050.TW": "元大台灣50", "0056.TW": "元大高股息", "6446.TW": "藥華藥",
}

SECTOR_MAP = {
    "2330.TW": "半導體", "2454.TW": "半導體", "2317.TW": "半導體",
    "2357.TW": "電子代工", "2382.TW": "電子代工", "2383.TW": "電子代工",
    "2882.TW": "金融", "2881.TW": "金融", "2891.TW": "金融",
    "1101.TW": "傳產", "1301.TW": "傳產", "1326.TW": "傳產",
    "7769.TW": "精密", "2049.TW": "精密", "1590.TW": "精密",
    "0050.TW": "ETF", "0056.TW": "ETF", "6446.TW": "其他",
}

START = "2021-01-01"
END = "2026-06-08"

INITIAL_CAPITAL = 1_000_000
COMMISSION = 0.001425
TAX = 0.003
STOP_LOSS_PCT = 8.0
TAKE_PROFIT_PCT = 20.0
BUY_THRESHOLD = 40
SELL_THRESHOLD = 40
WARMUP = 200

VARIANTS = ["baseline", "A_volume", "B_close_confirm", "C_kbar", "A+C", "B+C"]


# ── 資料抓取 ─────────────────────────────────────────────────────

def fetch_data(symbols: List[str]) -> Dict[str, pd.DataFrame]:
    out = {}
    for i, sym in enumerate(symbols):
        try:
            df = yf.Ticker(sym).history(start=START, end=END, interval="1d")
            if df.empty or len(df) < WARMUP + 50:
                print(f"  [{i+1}/{len(symbols)}] {sym}: 資料不足({len(df)})，跳過")
                continue
            df.columns = [c.lower() for c in df.columns]
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            df = df[df["volume"] > 0]
            df.index = pd.to_datetime(df.index.date)
            out[sym] = df
            print(f"  [{i+1}/{len(symbols)}] {sym}: {len(df)} 筆 ({df.index[0].date()} ~ {df.index[-1].date()})")
            time.sleep(0.8)  # yfinance rate limit
        except Exception as e:
            print(f"  [{i+1}/{len(symbols)}] {sym}: 抓取失敗 {e.__class__.__name__}")
    return out


# ── Filter（全當日判斷）─────────────────────────────────────────

def filter_volume(idx: int, df: pd.DataFrame) -> bool:
    """A: 當日 vol_ratio >= 1.5"""
    if idx < 20:
        return False
    vol = df["volume"].iloc[idx]
    avg20 = df["volume"].iloc[idx - 20:idx].mean()
    if avg20 <= 0:
        return False
    return vol / avg20 >= 1.5


def filter_close_confirm(idx: int, df: pd.DataFrame) -> bool:
    """B: 收盤 > 前 5 日 high × 1.005 + 收當日上半段 + 量配合"""
    if idx < 20:
        return False
    today = df.iloc[idx]
    prev5_high = df["high"].iloc[idx - 5:idx].max()
    if today["close"] < prev5_high * 1.005:
        return False
    day_range = today["high"] - today["low"]
    if day_range <= 0:
        return False
    if (today["close"] - today["low"]) / day_range < 0.5:
        return False
    return filter_volume(idx, df)


def filter_kbar(idx: int, df: pd.DataFrame) -> bool:
    """C: 紅 K 且實體佔比 >= 60%"""
    today = df.iloc[idx]
    if today["close"] <= today["open"]:
        return False
    body = today["close"] - today["open"]
    full = today["high"] - today["low"]
    if full <= 0:
        return False
    return body / full >= 0.6


def apply_filter(variant: str, idx: int, df: pd.DataFrame) -> bool:
    """True = filter 通過、可以買；False = 擋下"""
    if variant == "baseline":
        return True
    if variant == "A_volume":
        return filter_volume(idx, df)
    if variant == "B_close_confirm":
        return filter_close_confirm(idx, df)
    if variant == "C_kbar":
        return filter_kbar(idx, df)
    if variant == "A+C":
        return filter_volume(idx, df) and filter_kbar(idx, df)
    if variant == "B+C":
        return filter_close_confirm(idx, df) and filter_kbar(idx, df)
    return True


# ── 信號計算（沿用 production aggregator + regime layer） ────────

def precompute_signals(df: pd.DataFrame, symbol: str) -> List[Dict]:
    """逐日跑 aggregator + regime_layer，回傳 [{idx, buy, sell, regime, direction}]"""
    aggregator = SignalAggregator(market_type=MarketType.STOCK)
    regime_layer = RegimeLayer(enabled=True)

    results = []
    for i in range(WARMUP, len(df)):
        window = df.iloc[:i + 1].copy()
        try:
            calc_df = aggregator.calculate_all(window)
            sig = aggregator.generate_signals(calc_df, symbol, "1d")

            buy = float(sig.buy_score)
            sell = float(sig.sell_score)
            regime = ""

            try:
                mod = regime_layer.compute_modifier(symbol, calc_df)
                if mod.active:
                    buy = buy * mod.buy_multiplier + mod.buy_offset
                    sell = sell * mod.sell_multiplier + mod.sell_offset
                    regime = mod.regime or ""
                    if mod.veto_buy:
                        buy = min(buy, 10)
                    if mod.veto_sell:
                        sell = min(sell, 10)
            except Exception:
                pass

            buy = max(0, min(100, buy))
            sell = max(0, min(100, sell))
            if buy > sell:
                direction = "BUY"
            elif sell > buy:
                direction = "SELL"
            else:
                direction = "NEUTRAL"

            results.append({"idx": i, "buy": buy, "sell": sell,
                            "regime": regime, "direction": direction})
        except Exception:
            results.append({"idx": i, "buy": 0, "sell": 0,
                            "regime": "", "direction": "NEUTRAL"})
    return results


# ── 模擬交易 ─────────────────────────────────────────────────────

@dataclass
class Result:
    symbol: str
    name: str
    sector: str
    variant: str
    total_return_pct: float = 0.0
    annualized_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    win_trades: int = 0
    avg_hold_days: float = 0.0
    bnh_return_pct: float = 0.0
    final_capital: float = 0.0


def simulate(df: pd.DataFrame, signals: List[Dict], variant: str,
             symbol: str, name: str) -> Result:
    capital = INITIAL_CAPITAL
    peak = capital
    max_dd = 0.0
    position = None
    trades = []

    open_arr = df["open"].values
    close_arr = df["close"].values
    sig_map = {s["idx"]: s for s in signals}

    for i in range(WARMUP, len(df) - 1):
        sig = sig_map.get(i)
        if sig is None:
            continue

        if position is not None:
            cur_close = close_arr[i]
            pnl_pct = (cur_close - position["entry_price"]) / position["entry_price"] * 100

            should_sell = False
            if pnl_pct <= -STOP_LOSS_PCT:
                should_sell = True
            elif pnl_pct >= TAKE_PROFIT_PCT:
                should_sell = True
            elif sig["direction"] == "SELL" and sig["sell"] >= SELL_THRESHOLD:
                should_sell = True

            if should_sell:
                price = open_arr[i + 1]
                revenue = position["shares"] * price
                net = revenue * (1 - COMMISSION - TAX)
                capital += net
                pnl = net - position["shares"] * position["entry_price"]
                trades.append({
                    "entry_idx": position["entry_idx"],
                    "exit_idx": i + 1,
                    "pnl": pnl,
                    "hold_days": i + 1 - position["entry_idx"],
                })
                position = None

        elif sig["direction"] == "BUY" and sig["buy"] >= BUY_THRESHOLD:
            if apply_filter(variant, i, df):
                price = open_arr[i + 1]
                shares = int(capital * 0.95 / (price * (1 + COMMISSION)))
                if shares > 0:
                    cost = shares * price * (1 + COMMISSION)
                    capital -= cost
                    position = {
                        "entry_idx": i + 1,
                        "entry_price": price,
                        "shares": shares,
                    }

        equity = capital + (position["shares"] * close_arr[i] if position else 0)
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd

    if position is not None:
        price = close_arr[-1]
        revenue = position["shares"] * price
        net = revenue * (1 - COMMISSION - TAX)
        capital += net

    total_return = (capital / INITIAL_CAPITAL - 1) * 100
    days = (df.index[-1] - df.index[WARMUP]).days
    years = max(days / 365.25, 0.01)
    annualized = ((capital / INITIAL_CAPITAL) ** (1 / years) - 1) * 100

    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = (wins / len(trades) * 100) if trades else 0
    avg_hold = (sum(t["hold_days"] for t in trades) / len(trades)) if trades else 0

    bnh_price_start = open_arr[WARMUP + 1]
    bnh_price_end = close_arr[-1]
    bnh_return = (bnh_price_end / bnh_price_start - 1) * 100

    return Result(
        symbol=symbol, name=name, sector=SECTOR_MAP.get(symbol, "其他"),
        variant=variant,
        total_return_pct=round(total_return, 2),
        annualized_pct=round(annualized, 2),
        max_drawdown_pct=round(max_dd, 2),
        win_rate=round(win_rate, 1),
        total_trades=len(trades), win_trades=wins,
        avg_hold_days=round(avg_hold, 1),
        bnh_return_pct=round(bnh_return, 2),
        final_capital=round(capital),
    )


# ── 報告輸出 ─────────────────────────────────────────────────────

def save_csv(results: List[Result], ts: str):
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"filter_backtest_{ts}.csv")
    df = pd.DataFrame([r.__dict__ for r in results])
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"\nCSV 已存：{path}")
    return path


def save_markdown(results: List[Result], ts: str):
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results")
    path = os.path.join(out_dir, f"filter_backtest_{ts}.md")

    by_symbol: Dict[str, List[Result]] = {}
    for r in results:
        by_symbol.setdefault(r.symbol, []).append(r)

    lines = [
        f"# 反假突破/假跌破 Filter 回測報告",
        "",
        f"**期間**：{START} ~ {END}（約 5.5 年）  ",
        f"**生成時間**：{ts}  ",
        f"**變體**：{', '.join(VARIANTS)}  ",
        f"**初始資金**：{INITIAL_CAPITAL:,} TWD / 標的  ",
        f"**停損/停利**：{STOP_LOSS_PCT}% / {TAKE_PROFIT_PCT}%  ",
        f"**信號門檻**：buy/sell={BUY_THRESHOLD}",
        "",
        "---",
        "",
        "## 每檔股票推薦 filter（按 報酬/MDD 比值）",
        "",
        "| Symbol | Name | Sector | 推薦變體 | 報酬率 | MDD | 勝率 | 交易次數 | vs B&H |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for sym, rs in sorted(by_symbol.items()):
        def score(r):
            if r.max_drawdown_pct == 0:
                return r.total_return_pct
            return r.total_return_pct / max(r.max_drawdown_pct, 1)
        best = max(rs, key=score)
        diff = best.total_return_pct - best.bnh_return_pct
        diff_str = f"+{diff:.1f}" if diff >= 0 else f"{diff:.1f}"
        lines.append(
            f"| {sym} | {best.name} | {best.sector} | **{best.variant}** | "
            f"{best.total_return_pct:+.1f}% | -{best.max_drawdown_pct:.1f}% | "
            f"{best.win_rate:.0f}% | {best.total_trades} | {diff_str}pp |"
        )

    lines += ["", "---", "", "## 各變體 across all stocks", "",
              "| Variant | 平均報酬 | 平均 MDD | 平均勝率 | 總交易次數 |",
              "|---|---|---|---|---|"]
    for v in VARIANTS:
        vrs = [r for r in results if r.variant == v]
        if not vrs:
            continue
        lines.append(
            f"| {v} | {np.mean([r.total_return_pct for r in vrs]):+.1f}% | "
            f"-{np.mean([r.max_drawdown_pct for r in vrs]):.1f}% | "
            f"{np.mean([r.win_rate for r in vrs]):.0f}% | "
            f"{sum(r.total_trades for r in vrs)} |"
        )

    lines += ["", "---", "", "## 每檔股完整明細", ""]
    for sym, rs in sorted(by_symbol.items()):
        name = rs[0].name
        sector = rs[0].sector
        bnh = rs[0].bnh_return_pct
        lines.append(f"### {sym} {name}（{sector}, Buy&Hold: {bnh:+.1f}%）")
        lines.append("")
        lines.append("| Variant | 報酬 | 年化 | MDD | 勝率 | 交易 | 平均持有 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in sorted(rs, key=lambda x: -x.total_return_pct):
            lines.append(
                f"| {r.variant} | {r.total_return_pct:+.1f}% | "
                f"{r.annualized_pct:+.1f}% | -{r.max_drawdown_pct:.1f}% | "
                f"{r.win_rate:.0f}% | {r.total_trades} | {r.avg_hold_days:.0f}d |"
            )
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Markdown 已存：{path}")
    return path


# ── 主程式 ─────────────────────────────────────────────────────

def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print("=" * 70)
    print(f"反假突破/假跌破 Filter 回測  ({START} ~ {END})")
    print(f"Universe: {len(UNIVERSE)} 檔；變體 {len(VARIANTS)} 種")
    print("=" * 70)

    print("\n[1/3] 抓取 yfinance 資料...")
    data = fetch_data(list(UNIVERSE.keys()))
    if not data:
        print("⚠️  沒有任何標的抓到資料")
        return

    print(f"\n[2/3] 預計算每檔信號...")
    signals_cache: Dict[str, List[Dict]] = {}
    for sym, df in data.items():
        t0 = time.time()
        signals_cache[sym] = precompute_signals(df, sym)
        elapsed = time.time() - t0
        print(f"  {sym} 信號預計算 {elapsed:.1f}s ({len(signals_cache[sym])} 筆)")

    print(f"\n[3/3] 模擬 {len(data)} × {len(VARIANTS)} = {len(data) * len(VARIANTS)} 次回測...")
    results: List[Result] = []
    for sym, df in data.items():
        name = UNIVERSE[sym]
        signals = signals_cache[sym]
        for v in VARIANTS:
            r = simulate(df, signals, v, sym, name)
            results.append(r)
            print(f"  [{sym}/{v:18}] 報酬 {r.total_return_pct:+8.1f}% / "
                  f"MDD {r.max_drawdown_pct:6.1f}% / 交易 {r.total_trades:4d}")

    save_csv(results, ts)
    save_markdown(results, ts)
    print("\n✓ 完成")


if __name__ == "__main__":
    main()
