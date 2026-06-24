"""ETF vs 大盤 區間表現比較（漲跌雙向）。

用途：使用者隨時查詢「我的 ETF 在某段期間 vs 大盤」——
- 下跌窗口 → 看誰抗跌（跌幅小於大盤）
- 上漲窗口 → 看誰領漲（漲幅大於大盤）
核心指標「區間報酬」兩個方向通用；另附「區間內最大回撤」給下跌細節。

比較池 = BEAT_ETFS（9 檔 alpha 清單）+ 使用者自訂 watchlist（settings.json）。
價格來源用 yfinance（分析工具、非下單路徑，與系統 yfinance fallback 同源），
不耦合 broker / sinopac quote。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_BENCHMARK = "^TWII"   # 加權指數（大盤）
_BENCHMARK_NAMES = {"^TWII": "加權指數", "0050.TW": "元大台灣50"}


def _norm_etf_ticker(code: str) -> str:
    """'00981A' / '00981a.TW' / '00981A.TWO' → '00981A.TW'（ETF 皆上市）。

    注意：.TWO 必須先於 .TW 去除（.TW 是 .TWO 的子字串，順序反了會吃錯字元）。
    """
    c = code.strip().upper().replace(".TWO", "").replace(".TW", "")
    return c + ".TW"


def _extract_close(df) -> pd.Series | None:
    """從 yf.download 結果取單一 ticker 的 Close Series。"""
    if df is None or len(df) == 0 or "Close" not in df:
        return None
    close = df["Close"]
    if hasattr(close, "ndim") and close.ndim > 1:
        close = close.iloc[:, 0]
    return close.dropna()


def _fetch_closes(tickers: list[str], start: str, end: str) -> dict[str, pd.Series]:
    """批次抓多檔收盤。start 前多抓 10 天當基準日緩衝。"""
    import yfinance as yf

    s = (pd.to_datetime(start) - timedelta(days=10)).strftime("%Y-%m-%d")
    e = (pd.to_datetime(end) + timedelta(days=1)).strftime("%Y-%m-%d")
    out: dict[str, pd.Series] = {}
    try:
        raw = yf.download(tickers, start=s, end=e, progress=False, auto_adjust=True)
    except Exception as ex:
        logger.warning("etf_compare yf.download 失敗: %s", ex)
        return out
    if raw is None or len(raw) == 0:
        return out
    close_block = raw["Close"] if "Close" in raw else None
    if close_block is None:
        return out
    # 單檔時 close_block 是 Series；多檔是 DataFrame（欄=ticker）
    if isinstance(close_block, pd.Series):
        out[tickers[0]] = close_block.dropna()
    else:
        for t in tickers:
            if t in close_block.columns:
                s_ser = close_block[t].dropna()
                if len(s_ser):
                    out[t] = s_ser
    return out


def _close_asof(close: pd.Series, date: str) -> float:
    """date 當日（或之前最近交易日）的收盤；無則 NaN。"""
    sub = close.loc[:date].dropna()
    return float(sub.iloc[-1]) if len(sub) else float("nan")


def _base_close(close: pd.Series, start: str, end: str):
    """區間基準收盤。回 (price, date_str, partial)。

    - start 當日（或之前）有資料 → 用它、partial=False
    - 否則用窗口內第一筆（如 ETF 在窗口內才掛牌）→ partial=True、回報變「自掛牌起」
    - 窗口內完全無資料 → (NaN, None, False)
    """
    sub = close.loc[:start].dropna()
    if len(sub):
        return float(sub.iloc[-1]), str(sub.index[-1].date()), False
    win = close.loc[start:end].dropna()
    if len(win):
        return float(win.iloc[0]), str(win.index[0].date()), True
    return float("nan"), None, False


def _period_return(close: pd.Series, start: str, end: str) -> float:
    """區間報酬 %（end 收盤 / 基準收盤 - 1）。漲跌雙向通用。

    基準若 start 當日無資料（ETF 窗口內才掛牌）改用窗口內第一筆，避免誤判「無資料」。
    """
    p0, _, _ = _base_close(close, start, end)
    p1 = _close_asof(close, end)
    if not np.isfinite(p0) or not np.isfinite(p1) or p0 == 0:
        return float("nan")
    return (p1 / p0 - 1.0) * 100.0


def _max_drawdown(close: pd.Series, start: str, end: str) -> float:
    """區間內最大回撤 %（peak→trough，負值；無資料 NaN）。"""
    win = close.loc[start:end].dropna()
    if len(win) < 2:
        return float("nan")
    roll = win.cummax()
    dd = (win / roll - 1.0) * 100.0
    return float(dd.min())


def _coverage_days(close: pd.Series, start: str, end: str) -> int:
    return int(len(close.loc[start:end].dropna()))


def resolve_window(start: str | None, end: str | None) -> tuple[str, str]:
    """補預設窗口：end 預設今天、start 預設近 1 月。皆回 YYYY-MM-DD。"""
    today = datetime.now().strftime("%Y-%m-%d")
    end = (end or today)[:10]
    if not start:
        start = (pd.to_datetime(end) - timedelta(days=30)).strftime("%Y-%m-%d")
    return start[:10], end


def build_compare_pool() -> list[dict]:
    """合併 BEAT_ETFS（source=alpha）+ 使用者 watchlist（source=custom），依 code 去重。"""
    pool: list[dict] = []
    seen: set[str] = set()
    try:
        from layers.active_etf import BEAT_ETFS
        for e in BEAT_ETFS:
            code = e["code"].strip().upper()
            if code in seen:
                continue
            seen.add(code)
            pool.append({"code": code, "name": e.get("name", code), "source": "alpha"})
    except Exception as ex:
        logger.warning("讀 BEAT_ETFS 失敗: %s", ex)
    try:
        from settings_manager import get_watch_etfs
        for e in get_watch_etfs():
            code = e["code"].strip().upper()
            if code in seen:
                continue
            seen.add(code)
            pool.append({"code": code, "name": e.get("name", code), "source": "custom"})
    except Exception as ex:
        logger.warning("讀 watchlist 失敗: %s", ex)
    return pool


def compare(start: str | None = None, end: str | None = None,
            benchmark: str = DEFAULT_BENCHMARK) -> dict:
    """主入口：算比較池每檔 ETF 在 [start,end] 的區間報酬/最大回撤 vs 大盤。

    Returns:
        {
          "start", "end", "benchmark": {code,name,ret_pct,max_dd_pct},
          "etfs": [{code,name,source,ret_pct,max_dd_pct,n_days,
                    beat_ret(bool|None),beat_dd(bool|None),ok(bool)}],
        }
    beat_ret = 區間報酬贏大盤（漲更多 / 跌更少，雙向通用）；
    beat_dd  = 最大回撤小於大盤（較不深跌）。資料不足時為 None / ok=False。
    """
    start, end = resolve_window(start, end)
    pool = build_compare_pool()
    bench = benchmark or DEFAULT_BENCHMARK

    tickers = [_norm_etf_ticker(e["code"]) for e in pool] + [bench]
    closes = _fetch_closes(tickers, start, end)

    bclose = closes.get(bench)
    b_ret = _period_return(bclose, start, end) if bclose is not None else float("nan")
    b_dd = _max_drawdown(bclose, start, end) if bclose is not None else float("nan")

    def _round(v):
        return None if (v is None or not np.isfinite(v)) else round(float(v), 2)

    def _beat(v, base):
        """v 贏 base？（報酬更高 / 回撤更淺，皆 v>base）。回原生 bool 或 None。
        必須回原生 bool —— np.isfinite 等回 numpy.bool_，FastAPI 無法序列化。"""
        if not (np.isfinite(v) and np.isfinite(base)):
            return None
        return bool(v > base)

    etfs = []
    for e in pool:
        t = _norm_etf_ticker(e["code"])
        c = closes.get(t)
        if c is None or len(c) == 0:
            etfs.append({**e, "ok": False, "ret_pct": None, "max_dd_pct": None,
                         "n_days": 0, "beat_ret": None, "beat_dd": None})
            continue
        ret = _period_return(c, start, end)
        dd = _max_drawdown(c, start, end)
        _, base_date, partial = _base_close(c, start, end)
        etfs.append({
            **e, "ok": bool(np.isfinite(ret)),
            "ret_pct": _round(ret), "max_dd_pct": _round(dd),
            "n_days": _coverage_days(c, start, end),
            "beat_ret": _beat(ret, b_ret), "beat_dd": _beat(dd, b_dd),
            # ETF 在窗口內才掛牌時：報酬是「自 since 起」、非完整窗口（前端標註用）
            "partial": bool(partial), "since": base_date if partial else None,
        })

    # 預設依區間報酬由高到低排序（領漲在前；下跌窗口則抗跌在前）
    etfs.sort(key=lambda x: (x["ret_pct"] is not None, x["ret_pct"] or -1e9), reverse=True)

    return {
        "start": start, "end": end,
        "benchmark": {
            "code": bench, "name": _BENCHMARK_NAMES.get(bench, bench),
            "ret_pct": _round(b_ret), "max_dd_pct": _round(b_dd),
        },
        "etfs": etfs,
    }


def validate_etf(code: str) -> tuple[bool, str]:
    """驗證 ETF 代號在 yfinance 有資料、回 (ok, name_or_reason)。給新增時用。"""
    import yfinance as yf
    t = _norm_etf_ticker(code)
    try:
        df = yf.download(t, period="1mo", progress=False, auto_adjust=True)
        close = _extract_close(df)
        if close is None or len(close) == 0:
            return False, "no_data"
        # 試取名稱（抓不到就回代號）
        name = code.strip().upper()
        try:
            info = yf.Ticker(t).info
            name = info.get("longName") or info.get("shortName") or name
        except Exception:
            pass
        return True, name
    except Exception as ex:
        logger.warning("validate_etf %s 失敗: %s", code, ex)
        return False, "error"
