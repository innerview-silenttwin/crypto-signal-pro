"""
F3 + S1 上線驗證腳本（2026-05-21 起）

L1 預設：即時健檢（TAIEX regime / effective_buy_th / 持倉 highest_since_entry）
L2 (--history)：分析上線後 BUY/SELL，F3 是否生效、SELL 是否改成 S1 觸發
L3 (--counterfactual)：對每筆交易計算「若 F3/S1 沒上會發生什麼」

用法：
  python scripts/verify_f3_s1.py              # L1
  python scripts/verify_f3_s1.py --history    # L1 + L2
  python scripts/verify_f3_s1.py --counterfactual  # L1 + L2 + L3
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# 與 production 同源
from sector_auto_trader import fetch_taiex_regime, fetch_signal_data  # noqa: E402
from sector_trader import SECTOR_STOCKS, DEFAULT_STRATEGIES, SECTOR_IDS  # noqa: E402

ACCOUNTS_DIR = ROOT / "data" / "sector_accounts"
F3_S1_DEPLOY_DATE = "2026-05-21"  # PR 上線日


# ─────────────────────────────────────────────────────────────────
# L1: 即時健檢
# ─────────────────────────────────────────────────────────────────
def l1_health_check():
    print("=" * 70)
    print(f"L1 即時健檢 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # 1. TAIEX regime
    print("\n[1] TAIEX 大盤 regime")
    regime = fetch_taiex_regime()
    df = fetch_signal_data("^TWII", lookback_days=300)
    if df is not None and len(df) >= 200:
        close = float(df['close'].iloc[-1])
        ma50 = float(df['close'].rolling(50).mean().iloc[-1])
        ma200 = float(df['close'].rolling(200).mean().iloc[-1])
        d_ma200 = (close - ma200) / ma200 * 100
        d_ma50_200 = (ma50 - ma200) / ma200 * 100
        print(f"  目前 regime: {regime}")
        print(f"  TAIEX close = {close:.0f}")
        print(f"  MA50 = {ma50:.0f}, MA200 = {ma200:.0f}")
        print(f"  close vs MA200: {d_ma200:+.2f}%   MA50 vs MA200: {d_ma50_200:+.2f}%")
        # 邊界距離（離切換還多遠）
        if regime == "bull":
            dist_to_neutral = min(abs(d_ma200), abs(d_ma50_200))
            print(f"  → bull 維持中，最近的轉 neutral 邊界距離 {dist_to_neutral:.1f}%")
        elif regime == "neutral":
            print(f"  → ⚠ F3 啟用中：sector buy_th < 50 的會被收嚴到 50")
        else:
            print(f"  → bear，個股 RegimeLayer 會自動 veto_buy")
    else:
        print(f"  目前 regime: {regime} (TAIEX 資料不足)")

    # 2. 各 sector effective_buy_th
    print(f"\n[2] 各 sector 的 BUY 門檻（F3 後）")
    print(f"  {'Sector':<18} {'sector_buy_th':>14} {'effective':>10} {'change':>8}")
    for sector_name, cfg in DEFAULT_STRATEGIES.items():
        buy_th = cfg["buy_threshold"]
        effective = max(buy_th, 50) if regime == "neutral" else buy_th
        delta = f"+{effective - buy_th}" if effective > buy_th else "-"
        print(f"  {sector_name:<18} {buy_th:>14} {effective:>10} {delta:>8}")

    # 3. 各 sector 持倉 highest_since_entry 狀態
    print(f"\n[3] 各 sector 持倉 highest_since_entry 狀態")
    total_holdings = 0
    has_highest = 0
    missing_highest = []
    for sector_name, sector_id in SECTOR_IDS.items():
        p = ACCOUNTS_DIR / f"{sector_id}_account.json"
        if not p.exists():
            continue
        with open(p) as f:
            d = json.load(f)
        holdings = d.get("holdings", {})
        if not holdings:
            continue
        print(f"\n  [{sector_name}] {len(holdings)} 檔持倉")
        print(f"    {'symbol':<10} {'avg_price':>10} {'highest':>10} {'漲幅%':>8} {'狀態':>10}")
        for sym, h in holdings.items():
            qty = h.get("qty", 0)
            if qty <= 0:
                continue
            total_holdings += 1
            avg = h.get("avg_price", 0)
            highest = h.get("highest_since_entry")
            if highest is None:
                missing_highest.append((sector_name, sym))
                print(f"    {sym:<10} {avg:>10.1f} {'-':>10} {'-':>8} {'待補(舊資料)':>10}")
            else:
                has_highest += 1
                gain = (highest / avg - 1) * 100 if avg > 0 else 0
                # 合理性：highest >= avg_price，否則異常
                status = "OK" if highest >= avg else "⚠異常"
                print(f"    {sym:<10} {avg:>10.1f} {highest:>10.1f} {gain:>+7.1f}% {status:>10}")

    print(f"\n  總結：{total_holdings} 筆持倉，{has_highest} 已有 highest，"
          f"{len(missing_highest)} 待補")
    if missing_highest:
        print(f"  待補名單（下次 process_sector 跑到時會自動補）：")
        for sec, sym in missing_highest[:10]:
            print(f"    - {sec} / {sym}")

    # 4. PR 部署狀態
    print(f"\n[4] PR 部署資訊")
    print(f"  F3 entry filter: 已上線（commit 55ec46c, 2026-05-21）")
    print(f"  S1 trailing-stop SELL: 已上線（commit 94f6079, 2026-05-21）")
    print(f"  研究報告：scripts/backtest_exits/REPORT.md")


# ─────────────────────────────────────────────────────────────────
# L2: 上線後交易分析
# ─────────────────────────────────────────────────────────────────
_RE_TECH_SCORE = re.compile(r"技術(\d+)")
_RE_S1 = re.compile(r"S1.*?跌(\d+\.?\d*)×ATR")
_RE_S9 = re.compile(r"S9連3黑")
_RE_S8 = re.compile(r"S8.*?跌(\d+\.?\d*)×ATR")  # 舊 S8（應該不再出現）
_RE_STOPLOSS = re.compile(r"停損觸發")
_RE_TAKEPROFIT = re.compile(r"停利觸發")


def _parse_signal(signal: str) -> dict:
    """從交易 signal 描述抽出關鍵欄位"""
    out = {"raw": signal}
    if m := _RE_TECH_SCORE.search(signal):
        out["buy_score"] = int(m.group(1))
    if m := _RE_S1.search(signal):
        out["s1_atr_mult"] = float(m.group(1))
    if _RE_S9.search(signal):
        out["s9"] = True
    if m := _RE_S8.search(signal):
        out["s8_atr_mult"] = float(m.group(1))
    if _RE_STOPLOSS.search(signal):
        out["stoploss"] = True
    if _RE_TAKEPROFIT.search(signal):
        out["takeprofit"] = True
    return out


def _taiex_regime_at(date_str: str) -> str:
    """取得指定日期的 TAIEX regime（用本地 cache 或 fetch_signal_data 重算）"""
    df = fetch_signal_data("^TWII", lookback_days=300)
    if df is None or len(df) < 200:
        return "unknown"
    try:
        target = df.index[df.index.strftime('%Y-%m-%d') == date_str]
        if len(target) == 0:
            return "unknown"
        i = df.index.get_loc(target[0])
        if i < 200:
            return "unknown"
        close = float(df['close'].iloc[i])
        ma50 = float(df['close'].iloc[max(0, i - 49):i + 1].mean())
        ma200 = float(df['close'].iloc[max(0, i - 199):i + 1].mean())
        if close > ma200 and ma50 > ma200:
            return "bull"
        if close < ma200 and ma50 < ma200:
            return "bear"
        return "neutral"
    except Exception:
        return "unknown"


def l2_history_analysis():
    print("\n" + "=" * 70)
    print(f"L2 上線後交易分析（{F3_S1_DEPLOY_DATE} 起）")
    print("=" * 70)

    all_trades = []
    for sector_name, sector_id in SECTOR_IDS.items():
        p = ACCOUNTS_DIR / f"{sector_id}_account.json"
        if not p.exists():
            continue
        with open(p) as f:
            d = json.load(f)
        for h in d.get("history", []):
            t = h.get("time", "")
            if t < F3_S1_DEPLOY_DATE:
                continue
            all_trades.append({**h, "sector": sector_name})

    if not all_trades:
        print("\n  尚無上線後交易（{} 起）".format(F3_S1_DEPLOY_DATE))
        return

    buys = [t for t in all_trades if t["type"] == "BUY"]
    sells = [t for t in all_trades if t["type"] == "SELL"]

    # ── BUY 分析 ──
    print(f"\n[1] BUY 分析 ({len(buys)} 筆)")
    if buys:
        f3_violations = []
        for t in buys:
            parsed = _parse_signal(t.get("signal", ""))
            bs = parsed.get("buy_score")
            date = t.get("time", "")[:10]
            tregime = _taiex_regime_at(date)
            t["_parsed"] = parsed
            t["_taiex_regime"] = tregime
            if tregime == "neutral" and bs is not None and bs < 50:
                f3_violations.append(t)

        print(f"  {'date':<10} {'sector':<12} {'symbol':<10} "
              f"{'buy_score':>10} {'TAIEX regime':>14}")
        for t in buys[-10:]:  # 最近 10 筆
            parsed = t["_parsed"]
            bs = parsed.get("buy_score", "-")
            print(f"  {t['time'][:10]:<10} {t['sector']:<12} {t['symbol']:<10} "
                  f"{str(bs):>10} {t['_taiex_regime']:>14}")
        if len(buys) > 10:
            print(f"  ... 共 {len(buys)} 筆，僅顯示最近 10 筆")

        if f3_violations:
            print(f"\n  ❌ F3 漏網（TAIEX neutral 但 buy_score < 50 仍進場）: "
                  f"{len(f3_violations)} 筆")
            for t in f3_violations[:5]:
                print(f"    - {t['time'][:10]} {t['symbol']} buy_score={t['_parsed'].get('buy_score')}")
        else:
            print(f"\n  ✓ F3 守住：所有 TAIEX neutral 進場都符合 buy_score ≥ 50")

    # ── SELL 分析 ──
    print(f"\n[2] SELL 分析 ({len(sells)} 筆)")
    if sells:
        cats = {"S1_trailing": 0, "S9_red3": 0, "S8_舊": 0,
                "停損": 0, "停利": 0, "標準信號": 0, "其它": 0}
        s8_violations = []
        for t in sells:
            parsed = _parse_signal(t.get("signal", ""))
            if "s8_atr_mult" in parsed:
                cats["S8_舊"] += 1
                s8_violations.append(t)
            elif "s1_atr_mult" in parsed:
                cats["S1_trailing"] += 1
            elif parsed.get("s9"):
                cats["S9_red3"] += 1
            elif parsed.get("stoploss"):
                cats["停損"] += 1
            elif parsed.get("takeprofit"):
                cats["停利"] += 1
            elif "賣出信號" in t.get("signal", ""):
                cats["標準信號"] += 1
            else:
                cats["其它"] += 1

        print(f"  {'觸發類型':<18} {'筆數':>6}")
        for k, v in cats.items():
            if v > 0:
                marker = " ❌" if k == "S8_舊" else ""
                print(f"  {k:<18} {v:>6}{marker}")

        if s8_violations:
            print(f"\n  ❌ 仍有 S8 觸發：表示 PR 2 沒上或回滾了")
            for t in s8_violations[:5]:
                print(f"    - {t['time'][:10]} {t['symbol']}: {t['signal']}")
        elif cats["S1_trailing"] > 0:
            print(f"\n  ✓ S1 已生效：trailing stop 從持倉最高跌 N×ATR")


# ─────────────────────────────────────────────────────────────────
# L3: Counterfactual 對比
# ─────────────────────────────────────────────────────────────────
def l3_counterfactual():
    print("\n" + "=" * 70)
    print("L3 Counterfactual：F3/S1 vs 沒上線 假設對比")
    print("=" * 70)

    all_trades = []
    for sector_name, sector_id in SECTOR_IDS.items():
        p = ACCOUNTS_DIR / f"{sector_id}_account.json"
        if not p.exists():
            continue
        with open(p) as f:
            d = json.load(f)
        for h in d.get("history", []):
            t = h.get("time", "")
            if t < F3_S1_DEPLOY_DATE:
                continue
            all_trades.append({**h, "sector": sector_name})

    if not all_trades:
        print(f"\n  尚無上線後交易資料")
        return

    buys = [t for t in all_trades if t["type"] == "BUY"]

    # 對 BUY 計算「F3 救了幾筆」
    # 邏輯：若沒上 F3，TAIEX neutral 時 buy_score 介於 [sector_buy_th, 50) 的會進場
    # 但這是「F3 上線後實際發生的 BUY」分析 — 真正被擋的部位看不到（沒有 negative log）
    # 折衷：列出「擦邊」進場（TAIEX neutral 且 buy_score 50-55）做為參考
    print(f"\n[1] BUY 擦邊情境分析 (TAIEX neutral 時 buy_score 50-55)")
    edge = []
    for t in buys:
        parsed = _parse_signal(t.get("signal", ""))
        bs = parsed.get("buy_score")
        date = t.get("time", "")[:10]
        tregime = _taiex_regime_at(date)
        if tregime == "neutral" and bs is not None and 50 <= bs < 55:
            edge.append((t, bs))

    if edge:
        print(f"  {len(edge)} 筆 BUY 在 TAIEX neutral 且 buy_score 50-55 (F3 邊緣放行)")
        for t, bs in edge[:5]:
            print(f"    - {t['time'][:10]} {t['symbol']} buy_score={bs}")
        print(f"  → 觀察這些部位後續是否獲利，可驗證 F3 門檻 50 是否合適")
    else:
        print(f"  目前沒有擦邊筆數")

    # SELL counterfactual：要 daily snapshot 才精準（沒上線前的 highest_since_entry）
    print(f"\n[2] SELL counterfactual（需累積 ≥ 30 天再分析）")
    print(f"  TODO: 對每筆 S1 觸發 SELL，要紀錄當下「近 20 日高」")
    print(f"  才能計算「若用 S8 會不會也觸發」。需要每日 snapshot daemon。")
    print(f"  → 目前先用 L2 統計 S1 vs S9 vs 停損占比即可")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true", help="L2 上線後交易分析")
    parser.add_argument("--counterfactual", action="store_true", help="L3 對比")
    args = parser.parse_args()

    l1_health_check()
    if args.history or args.counterfactual:
        l2_history_analysis()
    if args.counterfactual:
        l3_counterfactual()


if __name__ == "__main__":
    main()
