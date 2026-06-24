"""籌碼 vs 未來大盤報酬：lead-lag 相關 + 期現背離條件分析（含 regime 分組）。

一次性研究腳本（非系統執行路徑）。結論見 memory/project_chip_leadlag_findings：
- 現貨三大法人買賣超對未來大盤報酬幾乎無領先性
- 期貨未平倉（尤投信）有微弱領先（解釋力 ~1%）
- 期現「背離A（現買期空）」全期/強勢最強，但弱勢 regime 樣本不足 → 多頭 artifact
- 空頭/弱勢結論資料不足（弱勢日僅 ~23%）
→ 籌碼當觀察/脈絡（背離、分歧），非預測訊號。

執行：
    python scripts/chip_analysis/chip_leadlag.py [起始日 YYYY-MM-DD]
需 requests / pandas / yfinance（dev venv 已具備）。
"""
import sys

import numpy as np
import pandas as pd
import requests

FM = "https://api.finmindtrade.com/api/v4/data"
START = sys.argv[1] if len(sys.argv) > 1 else "2025-01-01"


def fm(dataset, **kw):
    return requests.get(FM, params={"dataset": dataset, "start_date": START, **kw},
                        timeout=30).json().get("data", [])


def build_df():
    # 期貨 OI 淨額（外資 / 投信）
    fut = fm("TaiwanFuturesInstitutionalInvestors", data_id="TX")
    fr = {}
    for r in fut:
        net = (r.get("long_open_interest_balance_volume") or 0) - \
              (r.get("short_open_interest_balance_volume") or 0)
        fr.setdefault(r["date"], {})[r["institutional_investors"]] = net
    fdf = pd.DataFrame([{"date": d, "futF": v.get("外資"), "futT": v.get("投信")}
                        for d, v in fr.items()])

    # 現貨三大法人（億元）
    inst = fm("TaiwanStockTotalInstitutionalInvestors")
    ir = {}
    for r in inst:
        net = ((r.get("buy") or 0) - (r.get("sell") or 0)) / 1e8
        s = ir.setdefault(r["date"], {"cashF": 0, "cashT": 0})
        if r["name"] in ("Foreign_Investor", "Foreign_Dealer_Self"):
            s["cashF"] += net
        elif r["name"] == "Investment_Trust":
            s["cashT"] += net
    idf = pd.DataFrame([{"date": d, **v} for d, v in ir.items()])

    import yfinance as yf
    tw = yf.download("^TWII", start=START, progress=False, auto_adjust=True)
    cl = tw["Close"]
    cl = cl.iloc[:, 0] if cl.ndim > 1 else cl
    twdf = pd.DataFrame({"date": [str(i.date()) for i in cl.index], "twii": cl.values})

    df = twdf.merge(fdf, on="date").merge(idf, on="date").sort_values("date").reset_index(drop=True)
    df["futF_chg"] = df["futF"].diff()
    df["futT_chg"] = df["futT"].diff()
    df["ma20"] = df["twii"].rolling(20).mean()
    df["weak"] = df["twii"] < df["ma20"]
    for N in (1, 5, 10):
        df[f"fwd{N}"] = df["twii"].shift(-N) / df["twii"] - 1
    return df


def main():
    df = build_df()
    print(f"樣本 {df.date.iloc[0]}~{df.date.iloc[-1]}  {len(df)}日  弱勢日佔 {df.weak.mean()*100:.0f}%\n")

    # ── lead-lag 相關（rank-Pearson ≈ Spearman，免 scipy）──
    inds = {"外資期貨OI": "futF", "投信期貨OI": "futT", "外資現貨": "cashF", "投信現貨": "cashT"}
    print("=== Spearman 相關：指標 vs 未來 N 日大盤報酬 ===")
    print(f"{'指標':<12}" + "".join(f"{'fwd'+str(N):>8}" for N in (1, 5, 10)))
    for name, col in inds.items():
        row = ""
        for N in (1, 5, 10):
            sub = df[[col, f"fwd{N}"]].dropna()
            c = sub[col].rank().corr(sub[f"fwd{N}"].rank()) if len(sub) > 10 else np.nan
            row += f"{c:>8.3f}" if pd.notna(c) else f"{'NA':>8}"
        print(f"{name:<12}{row}")

    # ── 期現背離 × regime ──
    def rep(mask, label):
        s = df[mask].dropna(subset=["fwd5"])
        if len(s) < 8:
            return f"  {label:<22}n={len(s):<4}(樣本不足)"
        return f"  {label:<22}n={len(s):<4} 未來5日={s.fwd5.mean()*100:+.2f}% 勝率={(s.fwd5>0).mean()*100:.0f}%"

    for who, cash, futc in [("外資", "cashF", "futF_chg"), ("投信", "cashT", "futT_chg")]:
        print(f"\n=== {who} 期現背離（現貨方向 × 期貨OI變化）===")
        for rg, m in [("全期", pd.Series(True, index=df.index)), ("強勢", ~df.weak), ("弱勢", df.weak)]:
            print(f" 【{rg}】")
            print(rep(m & (df[cash] > 0) & (df[futc] > 0), "同向偏多"))
            print(rep(m & (df[cash] < 0) & (df[futc] < 0), "同向偏空"))
            print(rep(m & (df[cash] > 0) & (df[futc] < 0), "背離A 現買期空"))
            print(rep(m & (df[cash] < 0) & (df[futc] > 0), "背離B 現賣期多"))


if __name__ == "__main__":
    main()
