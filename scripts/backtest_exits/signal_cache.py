"""
Step 2: 對每檔股票，預先計算每日的 buy/sell score、regime、indicators，
存成 cache/signals/{symbol}.csv。後續 5 個 SELL 策略共用這份快取，省去重算成本。

設計：
- 對每檔 df 先一次性呼叫 calculate_all → 指標欄位全部填好（向量化）
- 然後對每一天 i：slice df.iloc[:i+1]，呼叫 generate_signals 和 RegimeLayer.compute_modifier
- 也手算 5 個 SELL 策略需要的額外欄位：MA10/20/50/200、EMA21、ATR14、MACD bear cross、
  vol_ma20、20d high、3-day red K 等
"""

import sys
import time
import warnings
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from signals.aggregator import SignalAggregator, MarketType  # noqa: E402
from layers.regime import RegimeLayer  # noqa: E402

from data_fetch import load_cached, all_symbols  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "cache" / "signals"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MIN_DATA_DAYS = 120
LOOKBACK_FOR_SIG = 200  # 每日 generate_signals 用的滾動視窗


def _add_extras(df: pd.DataFrame) -> pd.DataFrame:
    """補上 5 策略需要的欄位（向量化、一次算完）。"""
    d = df.copy()
    close = d['close']
    high = d['high']
    low = d['low']
    vol = d['volume']

    # 均線
    d['ma10'] = close.rolling(10).mean()
    d['ma20'] = close.rolling(20).mean()
    d['ma50'] = close.rolling(50).mean()
    d['ma200'] = close.rolling(200).mean()
    d['ema21'] = close.ewm(span=21, adjust=False).mean()

    # ATR14
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    d['atr14'] = tr.rolling(14).mean()

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    d['macd'] = ema12 - ema26
    d['macd_signal'] = d['macd'].ewm(span=9, adjust=False).mean()

    # 量能均線
    d['vol_ma20'] = vol.rolling(20).mean()

    # 近 20 日高點
    d['high20d'] = high.rolling(20).max()

    # 連 3 日紅黑判斷（簡化：close < open）
    red = (close < d['open']).astype(int)
    d['red3'] = red.rolling(3).sum() == 3

    return d


def compute_for_symbol(symbol: str, df_ohlcv: pd.DataFrame) -> pd.DataFrame:
    """跑完整單檔信號重建，回傳每日 row 的 DataFrame。"""
    if len(df_ohlcv) < MIN_DATA_DAYS:
        return pd.DataFrame()

    agg = SignalAggregator(market_type=MarketType.STOCK)
    regime = RegimeLayer(enabled=True)

    # 一次性算指標（向量化）
    df_calc = agg.calculate_all(df_ohlcv.copy())
    df_calc = _add_extras(df_calc)

    rows = []
    for i in range(MIN_DATA_DAYS, len(df_calc)):
        date = df_calc.index[i]
        window = df_calc.iloc[max(0, i - LOOKBACK_FOR_SIG): i + 1]
        try:
            sig = agg.generate_signals(window, symbol=symbol, timeframe="1d")
            raw_buy = float(sig.buy_score)
            raw_sell = float(sig.sell_score)
        except Exception:
            raw_buy = raw_sell = 0.0

        try:
            mod = regime.compute_modifier(symbol, window)
            r_active = mod.active
            r_name = mod.regime if r_active else ""
            r_buy_m = mod.buy_multiplier if r_active else 1.0
            r_sell_m = mod.sell_multiplier if r_active else 1.0
            r_veto = mod.veto_buy if r_active else False
        except Exception:
            r_name = ""
            r_buy_m = r_sell_m = 1.0
            r_veto = False

        buy_score = raw_buy * r_buy_m
        sell_score = raw_sell * r_sell_m

        row = {
            'date': date,
            'open': float(df_calc['open'].iloc[i]),
            'high': float(df_calc['high'].iloc[i]),
            'low': float(df_calc['low'].iloc[i]),
            'close': float(df_calc['close'].iloc[i]),
            'volume': float(df_calc['volume'].iloc[i]),
            'raw_buy': raw_buy,
            'raw_sell': raw_sell,
            'buy_score': buy_score,
            'sell_score': sell_score,
            'regime': r_name,
            'veto_buy': r_veto,
            'ma10': df_calc['ma10'].iloc[i],
            'ma20': df_calc['ma20'].iloc[i],
            'ma50': df_calc['ma50'].iloc[i],
            'ma200': df_calc['ma200'].iloc[i],
            'ema21': df_calc['ema21'].iloc[i],
            'atr14': df_calc['atr14'].iloc[i],
            'macd': df_calc['macd'].iloc[i],
            'macd_signal': df_calc['macd_signal'].iloc[i],
            'vol_ma20': df_calc['vol_ma20'].iloc[i],
            'high20d': df_calc['high20d'].iloc[i],
            'red3': bool(df_calc['red3'].iloc[i]),
        }
        rows.append(row)

    out = pd.DataFrame(rows).set_index('date')
    return out


def _path(symbol: str) -> Path:
    safe = symbol.replace("^", "_").replace(".", "_")
    return CACHE_DIR / f"{safe}.csv"


def cache_all(symbols=None, force: bool = False) -> Dict[str, pd.DataFrame]:
    """跑全部 / 部分 symbols 的信號快取。已存在的會 skip（除非 force）。"""
    syms = symbols or [s for s in all_symbols() if s != "^TWII"]
    ohlcv = load_cached(syms)
    out: Dict[str, pd.DataFrame] = {}

    for i, sym in enumerate(syms, 1):
        path = _path(sym)
        if not force and path.exists():
            try:
                out[sym] = pd.read_csv(path, index_col=0, parse_dates=[0])
                continue
            except Exception:
                pass

        df = ohlcv.get(sym)
        if df is None or len(df) < MIN_DATA_DAYS:
            print(f"  [{i}/{len(syms)}] {sym}: 資料不足，跳過")
            continue

        t0 = time.time()
        sig_df = compute_for_symbol(sym, df)
        if sig_df.empty:
            print(f"  [{i}/{len(syms)}] {sym}: 信號計算失敗，跳過")
            continue
        sig_df.to_csv(path)
        out[sym] = sig_df
        print(f"  [{i}/{len(syms)}] {sym}: {len(sig_df)} rows, {time.time()-t0:.1f}s")

    return out


def regime_taiex(force: bool = False) -> pd.DataFrame:
    """大盤 ^TWII 的 MA200 / 是否多頭 / 是否空頭 → 給 regime-aware combo 用"""
    path = _path("^TWII")
    if not force and path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=[0])

    d = load_cached(["^TWII"]).get("^TWII")
    if d is None:
        return pd.DataFrame()
    out = pd.DataFrame(index=d.index)
    out['close'] = d['close']
    out['ma50'] = d['close'].rolling(50).mean()
    out['ma200'] = d['close'].rolling(200).mean()
    out['bull'] = (out['close'] > out['ma200']) & (out['ma50'] > out['ma200'])
    out['bear'] = (out['close'] < out['ma200']) & (out['ma50'] < out['ma200'])
    out.to_csv(path)
    return out


def main():
    syms = [s for s in all_symbols() if s != "^TWII"]
    print(f"信號快取目標: {len(syms)} 檔")
    cache_all(syms)
    print("計算大盤 regime...")
    regime_taiex(force=True)
    print("完成")


if __name__ == "__main__":
    main()
