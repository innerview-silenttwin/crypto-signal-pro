"""
投組級回測引擎（5 策略 × 全部 sector）

每個策略獨立模擬一個投組：
- BUY 進場：buy_score >= 40 且 buy_score > sell_score 且 not veto_buy（所有策略共用，純比 SELL）
- SELL 出場（依序判定）：
    1. 停損% (sector default, 以 entry_price 為基準)
    2. 停利% (sector default)
    3. 標準綜合 SELL 信號（sell_score >= 40 且 sell_score > buy_score）
    4. 策略主動觸發（baseline / trailing / ma_break / adaptive / regime_combo）
- 每筆交易記錄：高點、退出時跌幅、TAIEX 段別、5 日反彈與否

輸出：
- scripts/backtest_exits/out/trades_{strategy}.csv
- scripts/backtest_exits/out/summary.csv（5 策略 × 各 metrics）
- scripts/backtest_exits/REPORT.md
"""

import sys
import time
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sector_trader import SECTOR_STOCKS, DEFAULT_STRATEGIES  # noqa: E402

from strategies import STRATEGIES, Position  # noqa: E402

CACHE_SIG_DIR = Path(__file__).resolve().parent / "cache" / "signals"
OUT_DIR = Path(__file__).resolve().parent / "out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 回測參數 ────────────────────────────────────────────────
INITIAL_CAPITAL = 1_000_000
POSITION_PCT = 0.05
MAX_POSITIONS = 20
FEE_BUY = 0.001425
FEE_SELL = 0.001425 + 0.003

BUY_THRESHOLD = 40.0      # 為了讓所有 sector 用同一基準
SELL_THRESHOLD = 40.0

BACKTEST_START = "2021-07-01"  # 有 200 日 lookback 後正式開始
LOOKAHEAD_FALSE_SIG = 5        # SELL 後幾天看反彈
FALSE_SIG_THRESHOLD = 0.05     # 反彈 ≥ 5% 即視為假訊號


# ── Symbol → sector 對照 ─────────────────────────────────────
def _symbol_to_sector() -> Dict[str, str]:
    out = {}
    for sec, stocks in SECTOR_STOCKS.items():
        for sym in stocks:
            out[sym] = sec
    return out


SYM2SECTOR = _symbol_to_sector()


def _sector_params(sym: str):
    sec = SYM2SECTOR.get(sym, "其他")
    cfg = DEFAULT_STRATEGIES.get(sec, DEFAULT_STRATEGIES["其他"])
    return {
        "stop_loss_pct": cfg["stop_loss_pct"] / 100.0,
        "take_profit_pct": cfg["take_profit_pct"] / 100.0,
        "sector": sec,
    }


# ── 載入訊號快取 ─────────────────────────────────────────────
def _load_signals() -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    for p in sorted(CACHE_SIG_DIR.glob("*.csv")):
        name = p.stem
        if name.startswith("_TWII"):
            continue
        sym = name.replace("_", ".", 1)
        try:
            df = pd.read_csv(p, index_col=0, parse_dates=[0])
            out[sym] = df
        except Exception as e:
            print(f"  ⚠ {sym} 讀取失敗: {e}")
    return out


def _load_taiex() -> pd.DataFrame:
    p = CACHE_SIG_DIR / "_TWII.csv"
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p, index_col=0, parse_dates=[0])


# ── Trade record ────────────────────────────────────────────
@dataclass
class TradeRecord:
    symbol: str
    sector: str
    strategy: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    highest_since_entry: float
    hold_days: int
    pnl_pct: float
    pnl: float
    giveback_from_peak_pct: float   # 從持倉最高點到出場的回落幅度
    exit_reason: str
    taiex_regime_at_entry: str      # bull / bear / neutral
    taiex_regime_at_exit: str
    stock_regime_at_entry: str      # 個股 RegimeLayer: 多頭/空頭/盤整/...
    stock_regime_at_exit: str
    false_signal: bool              # 出場後 5 日反彈 ≥ 5%


def _giveback(highest: float, exit_price: float) -> float:
    if highest <= 0:
        return 0.0
    return (highest - exit_price) / highest * 100.0


def _stock_regime(signals: Dict[str, pd.DataFrame], sym: str, date) -> str:
    df = signals.get(sym)
    if df is None or date not in df.index:
        return ""
    return str(df.loc[date].get('regime', '') or '')


def _taiex_tag(taiex: pd.DataFrame, date) -> str:
    if taiex.empty or date not in taiex.index:
        return "unknown"
    r = taiex.loc[date]
    if bool(r.get('bear', False)):
        return "bear"
    if bool(r.get('bull', False)):
        return "bull"
    return "neutral"


# ── 主回測 loop ──────────────────────────────────────────────
def run_backtest(strategy_name: str,
                 strategy_fn,
                 signals: Dict[str, pd.DataFrame],
                 taiex: pd.DataFrame,
                 use_f3: bool = False) -> Dict:
    """單一策略全宇宙投組回測

    use_f3: 啟用 F3 entry filter — TAIEX neutral 時 buy_score 必須 ≥ 50
    """
    # 統一日期軸（取所有 symbol 信號的日期聯集）
    all_dates = sorted(set(
        d for df in signals.values() for d in df.index
    ))
    start_dt = pd.Timestamp(BACKTEST_START)
    all_dates = [d for d in all_dates if d >= start_dt]

    capital = float(INITIAL_CAPITAL)
    positions: Dict[str, Position] = {}
    trade_log: List[TradeRecord] = []
    equity_curve = []
    per_pos_caps = []  # 持倉每日金額（含現金總資產）

    for date in all_dates:
        taiex_row = taiex.loc[date] if (not taiex.empty and date in taiex.index) else None

        # ── 1. 處理現有持倉 ──
        to_close = []
        for sym, pos in positions.items():
            df_sig = signals.get(sym)
            if df_sig is None or date not in df_sig.index:
                continue
            row = df_sig.loc[date]
            c = float(row['close'])

            # 更新最高價
            pos.highest_since_entry = max(pos.highest_since_entry, c)

            # 1a. 停損 / 停利
            sp = _sector_params(sym)
            pnl_pct = (c - pos.entry_price) / pos.entry_price
            if pnl_pct <= -sp["stop_loss_pct"]:
                to_close.append((sym, c, "停損"))
                continue
            if pnl_pct >= sp["take_profit_pct"]:
                to_close.append((sym, c, "停利"))
                continue

            # 1b. 標準綜合 SELL 信號
            bs = float(row.get('buy_score', 0) or 0)
            ss = float(row.get('sell_score', 0) or 0)
            if ss >= SELL_THRESHOLD and ss > bs:
                to_close.append((sym, c, f"std_sell({ss:.0f})"))
                continue

            # 1c. 策略觸發
            do_sell, reason = strategy_fn(row, pos, taiex_row)
            if do_sell:
                to_close.append((sym, c, reason))

        for sym, price, reason in to_close:
            pos = positions.pop(sym)
            proceeds = pos.shares * price * (1 - FEE_SELL)
            pnl = proceeds - pos.cost
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
            hold_days = (date - pos.entry_date).days
            capital += proceeds

            # 5 日後反彈檢查
            df_sig = signals.get(sym)
            false_sig = False
            if df_sig is not None and date in df_sig.index:
                idx = df_sig.index.get_loc(date)
                lookahead = df_sig.iloc[idx + 1: idx + 1 + LOOKAHEAD_FALSE_SIG]
                if not lookahead.empty:
                    max_close = lookahead['close'].max()
                    if max_close >= price * (1 + FALSE_SIG_THRESHOLD):
                        false_sig = True

            trade_log.append(TradeRecord(
                symbol=sym,
                sector=SYM2SECTOR.get(sym, "其他"),
                strategy=strategy_name,
                entry_date=str(pos.entry_date.date()),
                exit_date=str(date.date()),
                entry_price=round(pos.entry_price, 2),
                exit_price=round(price, 2),
                highest_since_entry=round(pos.highest_since_entry, 2),
                hold_days=hold_days,
                pnl_pct=round(pnl_pct, 2),
                pnl=round(pnl, 2),
                giveback_from_peak_pct=round(_giveback(pos.highest_since_entry, price), 2),
                exit_reason=reason,
                taiex_regime_at_entry=_taiex_tag(taiex, pos.entry_date),
                taiex_regime_at_exit=_taiex_tag(taiex, date),
                stock_regime_at_entry=_stock_regime(signals, sym, pos.entry_date),
                stock_regime_at_exit=_stock_regime(signals, sym, date),
                false_signal=false_sig,
            ))

        # ── 2. 掃描買入信號 ──
        slots = MAX_POSITIONS - len(positions)
        if slots > 0:
            # F3 filter: TAIEX neutral 時 buy_score 需 ≥ 50
            taiex_is_neutral = (taiex_row is not None
                                and not bool(taiex_row.get('bull', False))
                                and not bool(taiex_row.get('bear', False)))
            effective_buy_th = (
                50.0 if (use_f3 and taiex_is_neutral) else BUY_THRESHOLD
            )

            candidates = []
            for sym, df_sig in signals.items():
                if sym in positions:
                    continue
                if date not in df_sig.index:
                    continue
                row = df_sig.loc[date]
                bs = float(row.get('buy_score', 0) or 0)
                ss = float(row.get('sell_score', 0) or 0)
                veto = bool(row.get('veto_buy', False))
                if veto or bs < effective_buy_th or bs <= ss:
                    continue
                candidates.append((sym, bs, float(row['close'])))

            candidates.sort(key=lambda x: x[1], reverse=True)
            for sym, score, price in candidates[:slots]:
                invest = INITIAL_CAPITAL * POSITION_PCT
                cost = invest * (1 + FEE_BUY)
                if cost > capital:
                    continue
                shares = invest / price
                capital -= cost
                row = signals[sym].loc[date]
                positions[sym] = Position(
                    symbol=sym,
                    entry_date=date,
                    entry_price=price,
                    shares=shares,
                    cost=cost,
                    highest_since_entry=price,
                    atr14_at_entry=float(row.get('atr14', 0) or 0),
                )

        # ── 3. 當日總資產 ──
        holdings_value = 0.0
        for sym, pos in positions.items():
            df_sig = signals.get(sym)
            if df_sig is not None and date in df_sig.index:
                holdings_value += pos.shares * float(df_sig.loc[date, 'close'])
        total = capital + holdings_value
        equity_curve.append((date, total))

    # ── 期末強制平倉 ──
    if all_dates:
        last_date = all_dates[-1]
        for sym, pos in list(positions.items()):
            df_sig = signals.get(sym)
            if df_sig is None or last_date not in df_sig.index:
                continue
            price = float(df_sig.loc[last_date, 'close'])
            proceeds = pos.shares * price * (1 - FEE_SELL)
            capital += proceeds
            pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
            trade_log.append(TradeRecord(
                symbol=sym,
                sector=SYM2SECTOR.get(sym, "其他"),
                strategy=strategy_name,
                entry_date=str(pos.entry_date.date()),
                exit_date=str(last_date.date()),
                entry_price=round(pos.entry_price, 2),
                exit_price=round(price, 2),
                highest_since_entry=round(pos.highest_since_entry, 2),
                hold_days=(last_date - pos.entry_date).days,
                pnl_pct=round(pnl_pct, 2),
                pnl=round(proceeds - pos.cost, 2),
                giveback_from_peak_pct=round(_giveback(pos.highest_since_entry, price), 2),
                exit_reason="期末平倉",
                taiex_regime_at_entry=_taiex_tag(taiex, pos.entry_date),
                taiex_regime_at_exit=_taiex_tag(taiex, last_date),
                stock_regime_at_entry=_stock_regime(signals, sym, pos.entry_date),
                stock_regime_at_exit=_stock_regime(signals, sym, last_date),
                false_signal=False,
            ))

    return _summarize(strategy_name, trade_log, equity_curve)


# ── 績效彙整 ─────────────────────────────────────────────────
def _summarize(strategy_name: str, trades: List[TradeRecord],
               equity_curve) -> Dict:
    df_t = pd.DataFrame([asdict(t) for t in trades])
    if df_t.empty:
        return {"strategy": strategy_name, "n_trades": 0}

    eq = pd.Series([v for _, v in equity_curve])
    rm = eq.cummax()
    dd = (eq - rm) / rm * 100
    mdd = float(dd.min())

    final_cap = float(eq.iloc[-1])
    total_ret = (final_cap - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

    yrs = (equity_curve[-1][0] - equity_curve[0][0]).days / 365.25 if len(equity_curve) > 1 else 1
    ann = ((1 + total_ret / 100) ** (1 / yrs) - 1) * 100 if yrs > 0 else 0

    ret = eq.pct_change().dropna()
    sharpe = float((ret.mean() / ret.std()) * np.sqrt(252)) if ret.std() > 0 else 0.0

    wins = df_t[df_t['pnl'] > 0]
    losses = df_t[df_t['pnl'] <= 0]
    win_rate = len(wins) / len(df_t) * 100
    gp = wins['pnl'].sum()
    gl = abs(losses['pnl'].sum())
    pf = gp / gl if gl > 0 else float('inf')

    # bull / bear / neutral 分段（用 entry regime）
    bull = df_t[df_t['taiex_regime_at_entry'] == 'bull']
    bear = df_t[df_t['taiex_regime_at_entry'] == 'bear']
    neut = df_t[df_t['taiex_regime_at_entry'] == 'neutral']

    summary = {
        "strategy": strategy_name,
        "n_trades": int(len(df_t)),
        "total_return_pct": round(total_ret, 2),
        "annual_return_pct": round(ann, 2),
        "max_drawdown_pct": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(pf, 2) if pf != float('inf') else None,
        "avg_hold_days": round(df_t['hold_days'].mean(), 1),
        # 用戶關注的新指標
        "avg_giveback_from_peak_pct": round(df_t['giveback_from_peak_pct'].mean(), 2),
        "median_giveback_from_peak_pct": round(df_t['giveback_from_peak_pct'].median(), 2),
        "false_signal_rate_pct": round(df_t['false_signal'].mean() * 100, 2),
        # bull 段
        "bull_n": int(len(bull)),
        "bull_win_rate_pct": round(bull['pnl_pct'].gt(0).mean() * 100, 2) if len(bull) else None,
        "bull_avg_pnl_pct": round(bull['pnl_pct'].mean(), 2) if len(bull) else None,
        # bear 段（重點）
        "bear_n": int(len(bear)),
        "bear_win_rate_pct": round(bear['pnl_pct'].gt(0).mean() * 100, 2) if len(bear) else None,
        "bear_avg_pnl_pct": round(bear['pnl_pct'].mean(), 2) if len(bear) else None,
        "bear_avg_giveback_pct": round(bear['giveback_from_peak_pct'].mean(), 2) if len(bear) else None,
        # neutral
        "neutral_n": int(len(neut)),
        "neutral_avg_pnl_pct": round(neut['pnl_pct'].mean(), 2) if len(neut) else None,
        "_trade_df": df_t,
        "_equity_curve": equity_curve,
    }
    return summary


# ── 主程式 ──────────────────────────────────────────────────
def main(symbols=None):
    print("載入訊號快取...")
    signals = _load_signals()
    if symbols:
        signals = {s: signals[s] for s in symbols if s in signals}
    taiex = _load_taiex()
    print(f"  {len(signals)} 檔訊號，TAIEX 資料 {len(taiex)} 列")

    all_summaries = []
    for use_f3, suffix in [(False, ""), (True, "_f3")]:
        label = "Without F3" if not use_f3 else "With F3 (TAIEX neutral 時 buy_score ≥ 50)"
        print(f"\n──── {label} ────")
        for name, fn in STRATEGIES.items():
            display_name = f"{name}{suffix}"
            t0 = time.time()
            s = run_backtest(display_name, fn, signals, taiex, use_f3=use_f3)
            dt = time.time() - t0
            n = s.get("n_trades", 0)
            ret = s.get("total_return_pct", 0)
            mdd = s.get("max_drawdown_pct", 0)
            gb = s.get("avg_giveback_from_peak_pct", 0)
            print(f"  {display_name:<26} {dt:>5.1f}s  trades={n:<4} "
                  f"return={ret:>+6.1f}%  MDD={mdd:>5.1f}%  avg_giveback={gb:>5.1f}%")
            tdf = s.pop("_trade_df", pd.DataFrame())
            s.pop("_equity_curve", None)
            if not tdf.empty:
                tdf.to_csv(OUT_DIR / f"trades_{display_name}.csv",
                           index=False, encoding="utf-8-sig")
            all_summaries.append(s)

    summary_df = pd.DataFrame(all_summaries)
    summary_df.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n結果儲存於: {OUT_DIR}")
    return all_summaries


if __name__ == "__main__":
    main()
