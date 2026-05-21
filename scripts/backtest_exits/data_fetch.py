"""
Step 1: 拉 yfinance 5 年 OHLCV，快取到 cache/ohlcv/{symbol}.csv
涵蓋 76 檔 sector universe + ^TWII (大盤 regime filter 用)。
重複執行只會抓缺少或太舊（>24h）的資料。
"""

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sector_trader import SECTOR_STOCKS  # noqa: E402

CACHE_DIR = Path(__file__).resolve().parent / "cache" / "ohlcv"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

START_DATE = "2021-01-01"  # 涵蓋 2022 全年空頭、2024/8、2025/4
END_DATE = datetime.now().strftime("%Y-%m-%d")
STALE_HOURS = 24


def all_symbols() -> List[str]:
    syms: List[str] = []
    for stocks in SECTOR_STOCKS.values():
        syms.extend(stocks.keys())
    syms.append("^TWII")  # TAIEX
    # 去重保序
    seen = set()
    out = []
    for s in syms:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _path(symbol: str) -> Path:
    safe = symbol.replace("^", "_").replace(".", "_")
    return CACHE_DIR / f"{safe}.csv"


def _is_fresh(p: Path) -> bool:
    if not p.exists():
        return False
    age = time.time() - p.stat().st_mtime
    return age < STALE_HOURS * 3600


def fetch_one(symbol: str, force: bool = False) -> pd.DataFrame:
    p = _path(symbol)
    if not force and _is_fresh(p):
        return pd.read_csv(p, index_col=0, parse_dates=[0])
    df = yf.Ticker(symbol).history(start=START_DATE, end=END_DATE,
                                   interval="1d", auto_adjust=True)
    if df.empty:
        return df
    df.columns = [c.lower() for c in df.columns]
    df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
    df.index = df.index.tz_localize(None) if df.index.tz else df.index
    df.index.name = "date"
    df.to_csv(p)
    return df


def fetch_batch(symbols: List[str], force: bool = False) -> Dict[str, pd.DataFrame]:
    """批次下載；yfinance 多檔下載比逐檔快很多。"""
    need_fetch = [s for s in symbols if force or not _is_fresh(_path(s))]
    cached_only = [s for s in symbols if s not in need_fetch]

    out: Dict[str, pd.DataFrame] = {}
    for s in cached_only:
        try:
            out[s] = pd.read_parquet(_path(s))
        except Exception:
            need_fetch.append(s)

    if need_fetch:
        print(f"  下載 {len(need_fetch)}/{len(symbols)} 檔（其他已快取）...")
        raw = yf.download(need_fetch, start=START_DATE, end=END_DATE,
                          auto_adjust=True, progress=False, threads=True)
        if isinstance(raw.columns, pd.MultiIndex):
            for sym in need_fetch:
                try:
                    df = raw.xs(sym, axis=1, level=1).copy()
                except Exception:
                    continue
                df.columns = [c.lower() for c in df.columns]
                df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
                df.index = df.index.tz_localize(None) if df.index.tz else df.index
                if len(df) >= 120:
                    df.index.name = "date"
                    df.to_csv(_path(sym))
                    out[sym] = df
        else:
            sym = need_fetch[0]
            df = raw.copy()
            df.columns = [c.lower() for c in df.columns]
            df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
            df.index = df.index.tz_localize(None) if df.index.tz else df.index
            if len(df) >= 120:
                df.index.name = "date"
                df.to_csv(_path(sym))
                out[sym] = df
    return out


def load_cached(symbols: List[str]) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    missing: List[str] = []
    for s in symbols:
        p = _path(s)
        if p.exists():
            try:
                out[s] = pd.read_csv(p, index_col=0, parse_dates=[0])
                continue
            except Exception:
                pass
        missing.append(s)
    if missing:
        print(f"  尚未快取的 {len(missing)} 檔: {missing[:5]}{'...' if len(missing)>5 else ''}")
    return out


def main():
    syms = all_symbols()
    print(f"目標: {len(syms)} 檔（含 ^TWII），起始 {START_DATE}")
    t0 = time.time()
    data = fetch_batch(syms)
    print(f"完成: {len(data)}/{len(syms)} 檔可用，耗時 {time.time()-t0:.1f}s")
    short = [s for s, d in data.items() if len(d) < 800]
    if short:
        print(f"⚠ {len(short)} 檔資料量 < 800 日: {short[:10]}")


if __name__ == "__main__":
    main()
