#!/usr/bin/env python3
"""sinopac_data_probe.py — 比對 Shioaji 與 yfinance 行情資料差異

目的：在 Phase 2 切換行情源前，先量化兩個資料源在同檔同期間的差異，
判斷切換後是否需要重做評分權重回測。

跑這個腳本**不會切換現有系統**，純粹是測量。

樣本：
- 10 檔股票，跨類股（半導體 / 電子 / 金融 / 傳產 / 電信 / ETF / 小型股）
- 30 個交易日的日線（Open / High / Low / Close / Volume）
- 即時 snapshot（盤中 vs yfinance 最新）

輸出：
- 每檔股票的 mean / max % 差異
- 整體 1500 個數據點的 95p 差異
- snapshot 即時延遲落差

執行：
    python3 scripts/sinopac_data_probe.py
"""
from __future__ import annotations

import logging
import os
import sys
import statistics
from datetime import datetime, timedelta
from pathlib import Path

# ── .env 與 SSL 修補（與 sinopac_signed_test.py 同模式）──
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass

if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass

logging.getLogger("shioaji").setLevel(logging.WARNING)
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


# ── 樣本股票（10 檔，跨類股）──
SAMPLE = [
    ("2330", "台積電",     "semiconductor"),
    ("2454", "聯發科",     "semiconductor"),
    ("2317", "鴻海",       "electronics"),
    ("2382", "廣達",       "electronics"),
    ("2308", "台達電",     "electronics"),
    ("2881", "富邦金",     "financial"),
    ("1301", "台塑",       "traditional"),
    ("2412", "中華電",     "telecom"),
    ("0050", "元大台灣50", "etf"),
    ("3034", "聯詠",       "small_cap"),
]

LOOKBACK_DAYS = 45  # 抓 45 個自然日 → 通常 ≈ 30 個交易日


def _ok(m): print(f"✅ {m}")
def _info(m): print(f"   {m}")
def _warn(m): print(f"⚠️  {m}")
def _fail(m, code=1):
    print(f"\n❌ {m}")
    sys.exit(code)


def _pct(a: float, b: float) -> float:
    """回傳 |a-b| / b * 100；b=0 時回 0。"""
    if b == 0:
        return 0.0
    return abs(a - b) / abs(b) * 100.0


def fetch_shioaji_daily(api, code: str, start: str, end: str):
    """從 Shioaji 抓日線 — kbars 是 1 分鐘，要 resample 成日線。

    回傳 dict[date_str] = {"open", "high", "low", "close", "volume"}
    """
    import pandas as pd

    try:
        contract = api.Contracts.Stocks[code]
    except Exception as e:
        _warn(f"{code} 取合約失敗：{e.__class__.__name__}")
        return {}

    try:
        kbars = api.kbars(contract=contract, start=start, end=end)
    except Exception as e:
        _warn(f"{code} kbars() 失敗：{e.__class__.__name__}: {e}")
        return {}

    # kbars 物件可轉成 dict / dataframe；shioaji 1.x: kbars 有 .ts/.Open/.High/.Low/.Close/.Volume 屬性
    try:
        df = pd.DataFrame({**kbars})
        df["ts"] = pd.to_datetime(df["ts"])
        df["date"] = df["ts"].dt.strftime("%Y-%m-%d")
    except Exception as e:
        _warn(f"{code} kbars dataframe 轉換失敗：{e}")
        return {}

    if df.empty:
        return {}

    # 1 分鐘 K → 日線：第一筆 Open、最大 High、最小 Low、最後 Close、Volume 加總
    daily = df.groupby("date").agg(
        open=("Open", "first"),
        high=("High", "max"),
        low=("Low", "min"),
        close=("Close", "last"),
        volume=("Volume", "sum"),
    )

    return {d: dict(row) for d, row in daily.to_dict("index").items()}


def fetch_yfinance_daily(code: str, start: str, end: str):
    """從 yfinance 抓日線；TW 股需要 .TW 後綴。"""
    import yfinance as yf

    yf_symbol = f"{code}.TW" if not code.startswith("0050") else "0050.TW"
    try:
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(start=start, end=end, auto_adjust=False)
    except Exception as e:
        _warn(f"{code} yfinance 失敗：{e.__class__.__name__}")
        return {}

    if hist is None or hist.empty:
        return {}

    out = {}
    for ts, row in hist.iterrows():
        d = ts.strftime("%Y-%m-%d")
        out[d] = {
            "open":   float(row["Open"]),
            "high":   float(row["High"]),
            "low":    float(row["Low"]),
            "close":  float(row["Close"]),
            "volume": float(row["Volume"]),
        }
    return out


def fetch_shioaji_snapshots(api, codes: list[str]) -> dict[str, dict]:
    """單一 API call 抓所有股票的當前 snapshot。"""
    try:
        contracts = [api.Contracts.Stocks[c] for c in codes]
    except Exception as e:
        _warn(f"snapshot 取合約失敗：{e.__class__.__name__}")
        return {}

    try:
        snaps = api.snapshots(contracts)
    except Exception as e:
        _warn(f"snapshots() 失敗：{e.__class__.__name__}: {e}")
        return {}

    out = {}
    for s in snaps:
        # snapshot 欄位通常含 code / close / volume / total_volume
        code = getattr(s, "code", None) or getattr(s, "symbol", None)
        if not code:
            continue
        out[code] = {
            "close":  float(getattr(s, "close", 0) or 0),
            "volume": float(getattr(s, "total_volume", 0) or getattr(s, "volume", 0) or 0),
            "ts":     str(getattr(s, "ts", "")),
        }
    return out


def fetch_yfinance_snapshots(codes: list[str]) -> dict[str, dict]:
    import yfinance as yf
    out = {}
    for c in codes:
        yf_sym = f"{c}.TW"
        try:
            t = yf.Ticker(yf_sym)
            fi = t.fast_info
            out[c] = {
                "close":  float(fi.get("last_price", 0) or 0),
                "volume": float(fi.get("last_volume", 0) or 0),
            }
        except Exception as e:
            _warn(f"{c} yfinance snapshot 失敗：{e.__class__.__name__}")
    return out


def main() -> int:
    print("=" * 70)
    print("Shioaji vs yfinance 行情資料差異探測")
    print("=" * 70)

    # ── 環境 ──
    api_key = os.environ.get("SHIOAJI_API_KEY", "").strip()
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "").strip()
    ca_path = os.environ.get("SHIOAJI_CA_PATH", "").strip()
    ca_password = os.environ.get("SHIOAJI_CA_PASSWORD", "").strip()
    person_id = os.environ.get("SHIOAJI_PERSON_ID", "").strip()

    if not (api_key and secret_key):
        _fail("缺 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY；請先設定 .env")

    # ── 套件 ──
    try:
        import shioaji as sj
    except ImportError:
        _fail("shioaji 未安裝", code=2)

    try:
        import pandas  # noqa: F401
    except ImportError:
        _fail("pandas 未安裝（pip install pandas）", code=2)

    try:
        import yfinance  # noqa: F401
    except ImportError:
        _fail("yfinance 未安裝（pip install yfinance）", code=2)

    print(f"\n── shioaji {getattr(sj, '__version__', '?')} ──")
    print(f"── 樣本：{len(SAMPLE)} 檔股票、最近 {LOOKBACK_DAYS} 自然日 ──\n")

    # ── 登入 + CA ──
    print("➡️  登入 Shioaji（simulation=True）")
    api = sj.Shioaji(simulation=True)
    try:
        api.login(api_key=api_key, secret_key=secret_key, fetch_contract=False)
    except Exception as e:
        _fail(f"登入失敗：{e.__class__.__name__}: {e}", code=3)
    _ok("登入成功")

    if ca_path and ca_password:
        try:
            try:
                api.activate_ca(ca_path=ca_path, ca_passwd=ca_password)
            except TypeError:
                api.activate_ca(ca_path=ca_path, ca_passwd=ca_password, person_id=person_id)
            _ok("CA 已啟用")
        except Exception as e:
            _warn(f"activate_ca 失敗（snapshot 與 kbars 可能不受影響）：{e.__class__.__name__}")

    print("\n➡️  fetch_contracts(...)")
    try:
        api.fetch_contracts(contract_download=True)
        _ok("contract fetch 完成")
    except Exception as e:
        _warn(f"fetch_contracts：{e.__class__.__name__}")

    # ── 日線比對 ──
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    print(f"\n➡️  日線範圍：{start_date} ~ {end_date}")

    all_diffs_close = []
    all_diffs_open = []
    all_diffs_high = []
    all_diffs_low = []
    all_diffs_volume = []
    per_stock_report = []

    for code, name, sector in SAMPLE:
        print(f"\n── {code} {name} ({sector}) ──")
        sj_daily = fetch_shioaji_daily(api, code, start_date, end_date)
        yf_daily = fetch_yfinance_daily(code, start_date, end_date)

        if not sj_daily or not yf_daily:
            print(f"   ❌ 跳過（sj={len(sj_daily)} yf={len(yf_daily)}）")
            continue

        common_dates = sorted(set(sj_daily.keys()) & set(yf_daily.keys()))
        if not common_dates:
            print(f"   ❌ 無共同日期")
            continue

        close_diffs = []
        open_diffs = []
        high_diffs = []
        low_diffs = []
        vol_diffs = []
        for d in common_dates:
            s = sj_daily[d]
            y = yf_daily[d]
            close_diffs.append(_pct(s["close"], y["close"]))
            open_diffs.append(_pct(s["open"], y["open"]))
            high_diffs.append(_pct(s["high"], y["high"]))
            low_diffs.append(_pct(s["low"], y["low"]))
            vol_diffs.append(_pct(s["volume"], y["volume"]))

        all_diffs_close.extend(close_diffs)
        all_diffs_open.extend(open_diffs)
        all_diffs_high.extend(high_diffs)
        all_diffs_low.extend(low_diffs)
        all_diffs_volume.extend(vol_diffs)

        mean_c = statistics.mean(close_diffs)
        max_c = max(close_diffs)
        max_idx = close_diffs.index(max_c)
        max_date = common_dates[max_idx]

        per_stock_report.append({
            "code": code, "name": name,
            "n_days": len(common_dates),
            "mean_close_pct": mean_c,
            "max_close_pct": max_c,
            "max_date": max_date,
            "mean_vol_pct": statistics.mean(vol_diffs),
        })

        print(f"   日數 {len(common_dates)} | 收盤 mean {mean_c:.4f}% / max {max_c:.4f}% ({max_date}) | 量 mean {statistics.mean(vol_diffs):.2f}%")

    # ── snapshot 比對 ──
    print("\n➡️  Snapshot 即時比對")
    codes = [s[0] for s in SAMPLE]
    sj_snap = fetch_shioaji_snapshots(api, codes)
    yf_snap = fetch_yfinance_snapshots(codes)

    snap_report = []
    for code, name, _ in SAMPLE:
        s = sj_snap.get(code)
        y = yf_snap.get(code)
        if not s or not y or s["close"] == 0 or y["close"] == 0:
            snap_report.append((code, name, None, None, None))
            continue
        diff = _pct(s["close"], y["close"])
        snap_report.append((code, name, s["close"], y["close"], diff))

    # ── 摘要 ──
    print("\n" + "=" * 70)
    print("【日線差異摘要】")
    print("=" * 70)
    print(f"{'代號':<6}{'名稱':<8}{'天數':<6}{'收盤mean%':<12}{'收盤max%':<12}{'最大日':<12}{'量mean%':<10}")
    print("-" * 70)
    for r in per_stock_report:
        print(f"{r['code']:<6}{r['name']:<8}{r['n_days']:<6}{r['mean_close_pct']:<12.4f}{r['max_close_pct']:<12.4f}{r['max_date']:<12}{r['mean_vol_pct']:<10.2f}")

    def _stats(name, arr):
        if not arr:
            print(f"   {name}: (no data)")
            return
        arr_sorted = sorted(arr)
        p95 = arr_sorted[int(len(arr_sorted) * 0.95)]
        print(f"   {name}: n={len(arr)} mean={statistics.mean(arr):.4f}% p95={p95:.4f}% max={max(arr):.4f}%")

    print("\n【整體統計】")
    _stats("收盤", all_diffs_close)
    _stats("開盤", all_diffs_open)
    _stats("最高", all_diffs_high)
    _stats("最低", all_diffs_low)
    _stats("量",   all_diffs_volume)

    print("\n【Snapshot 即時】")
    print(f"{'代號':<6}{'名稱':<8}{'Shioaji':<12}{'yfinance':<12}{'差異%':<10}")
    print("-" * 50)
    for code, name, sc, yc, diff in snap_report:
        if diff is None:
            print(f"{code:<6}{name:<8}{'(n/a)':<12}{'(n/a)':<12}{'-':<10}")
        else:
            print(f"{code:<6}{name:<8}{sc:<12.2f}{yc:<12.2f}{diff:<10.4f}")

    # ── 結論 ──
    print("\n" + "=" * 70)
    if all_diffs_close:
        mean_close = statistics.mean(all_diffs_close)
        if mean_close < 0.5:
            print("🟢 收盤 mean 差異 < 0.5%：Phase 2 切換風險低")
        elif mean_close < 1.0:
            print("🟡 收盤 mean 差異 0.5–1.0%：切換可行但建議部分指標重做回測")
        else:
            print("🔴 收盤 mean 差異 > 1.0%：切換前必須重做回測")
    print("=" * 70)

    try:
        api.logout()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
