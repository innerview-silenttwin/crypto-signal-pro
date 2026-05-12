#!/usr/bin/env python3
"""recapitalize_and_cleanup_dust.py — 加碼本金 + 補足 dust positions

針對指定 sector 帳戶：
  1. balance 加碼指定金額（預設 1,000,000）
  2. initial_balance 同步加碼（影響 kill-switch 門檻）
  3. 所有持倉 < 10 股的 dust 部位，補足到 10 股
     - 補足用持倉原本的 avg_price（虛擬補貨，不抓即時）
     - 含手續費 0.1425% 計入 total_cost
  4. 寫入 history 紀錄（deposit + topup 各一筆）
  5. atomic save（先寫 .tmp 再 rename，並備份原檔）

執行：
    # 預覽（dry-run，不寫檔）
    python3 scripts/recapitalize_and_cleanup_dust.py

    # 真的執行
    python3 scripts/recapitalize_and_cleanup_dust.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# 預設參數
DEFAULT_SECTORS = ["semiconductor", "electronics"]
DEFAULT_DEPOSIT = 1_000_000.0
DUST_THRESHOLD = 10        # < 10 股視為 dust
TARGET_LOT = 10            # 補足到 10 股
TW_FEE_RATE = 0.001425     # 手續費（買賣皆有）

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = PROJECT_ROOT / "data" / "sector_accounts"


def _info(msg): print(f"  {msg}")
def _ok(msg): print(f"✅ {msg}")
def _warn(msg): print(f"⚠️  {msg}")
def _fail(msg, code=1):
    print(f"❌ {msg}")
    sys.exit(code)


def atomic_write_json(path: Path, data: dict) -> None:
    """tempfile + os.replace 原子寫入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def process_account(sector: str, deposit: float, apply: bool) -> dict:
    """處理單一 sector 帳戶。回傳 summary dict。"""
    path = ACCOUNTS_DIR / f"{sector}_account.json"
    if not path.exists():
        _warn(f"{sector}: 找不到 {path}")
        return {"sector": sector, "skipped": True}

    with open(path) as f:
        state = json.load(f)

    old_balance = float(state.get("balance", 0))
    old_initial = float(state.get("initial_balance", 0))
    holdings = state.get("holdings", {}) or {}

    # 1. 找 dust
    dust_list = []
    for sym, h in holdings.items():
        qty = int(h.get("qty", 0) or 0)
        if 0 < qty < DUST_THRESHOLD:
            avg = float(h.get("avg_price", 0) or 0)
            need = TARGET_LOT - qty
            cost = need * avg * (1 + TW_FEE_RATE)
            dust_list.append({
                "symbol": sym,
                "current_qty": qty,
                "need_qty": need,
                "avg_price": avg,
                "topup_cost": cost,
            })

    total_topup_cost = sum(x["topup_cost"] for x in dust_list)

    print(f"\n── {sector} ──")
    _info(f"加碼前：balance=${old_balance:,.0f}, initial_balance=${old_initial:,.0f}")
    _info(f"+ 加碼 ${deposit:,.0f}")
    new_balance = old_balance + deposit
    new_initial = old_initial + deposit
    _info(f"加碼後：balance=${new_balance:,.0f}, initial_balance=${new_initial:,.0f}")

    if not dust_list:
        _info("無 dust position，僅加碼")
        if apply:
            state["balance"] = round(new_balance, 2)
            state["initial_balance"] = round(new_initial, 2)
            state.setdefault("history", []).insert(0, {
                "id": int(time.time() * 1000),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "DEPOSIT",
                "amount": round(deposit, 2),
                "balance_after": round(new_balance, 2),
                "signal": f"capital injection +${int(deposit):,}",
            })
        return {"sector": sector, "deposit": deposit, "dust": 0, "topup_cost": 0}

    _info(f"{len(dust_list)} 筆 dust 要補足，預估成本 ~${total_topup_cost:,.0f}")
    for x in dust_list:
        _info(f"   {x['symbol']}: {x['current_qty']}→{TARGET_LOT} 股 @${x['avg_price']:,.2f} "
              f"→ +{x['need_qty']} 股 cost ~${x['topup_cost']:,.0f}")

    if new_balance < total_topup_cost:
        _warn(f"加碼後 balance ${new_balance:,.0f} < 補足成本 ${total_topup_cost:,.0f}，將會超支！")
        # 不過實際補貨會 per-symbol 檢查，下面會處理

    # 2. 模擬 / 套用 topup
    if not apply:
        _info("(dry-run，未實際寫檔)")
        return {
            "sector": sector,
            "deposit": deposit,
            "dust": len(dust_list),
            "topup_cost": total_topup_cost,
            "details": dust_list,
        }

    state["balance"] = round(new_balance, 2)
    state["initial_balance"] = round(new_initial, 2)
    state.setdefault("history", []).insert(0, {
        "id": int(time.time() * 1000),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": "DEPOSIT",
        "amount": round(deposit, 2),
        "balance_after": round(new_balance, 2),
        "signal": f"capital injection +${int(deposit):,}",
    })

    skipped_topups = []
    for x in dust_list:
        if state["balance"] < x["topup_cost"]:
            skipped_topups.append(x)
            _warn(f"   {x['symbol']}: balance 不夠 (${state['balance']:,.0f} < ${x['topup_cost']:,.0f})，跳過")
            continue

        sym = x["symbol"]
        h = state["holdings"][sym]
        old_qty = h["qty"]
        old_total_cost = float(h.get("total_cost", old_qty * h.get("avg_price", 0) * (1 + TW_FEE_RATE)))

        new_qty = TARGET_LOT
        # avg_price 不變（同價補貨），total_cost 累加
        new_total_cost = round(old_total_cost + x["topup_cost"], 2)

        state["holdings"][sym] = {
            **h,
            "qty": new_qty,
            "total_cost": new_total_cost,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        state["balance"] = round(state["balance"] - x["topup_cost"], 2)

        # history 記一筆 dust_topup
        state["history"].insert(0, {
            "id": int(time.time() * 1000) + len(state["history"]),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sym,
            "name": sym,  # 不查 stock_name table；以 symbol 暫代
            "type": "BUY",
            "subtype": "dust_topup",
            "price": x["avg_price"],
            "qty": x["need_qty"],
            "cost": round(x["topup_cost"], 2),
            "signal": f"dust topup ({x['current_qty']}→{TARGET_LOT} 股)",
            "balance_after": state["balance"],
        })

    # 3. 備份原檔 + atomic write
    backup_path = path.with_suffix(f".json.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, backup_path)
    _ok(f"備份 → {backup_path.name}")

    atomic_write_json(path, state)
    _ok(f"已套用 → {path.name}（balance ${state['balance']:,.0f}, initial ${state['initial_balance']:,.0f}）")

    return {
        "sector": sector,
        "deposit": deposit,
        "dust": len(dust_list),
        "topup_cost": total_topup_cost,
        "skipped_topups": len(skipped_topups),
    }


def main():
    ap = argparse.ArgumentParser(description="加碼本金 + 補足 dust positions")
    ap.add_argument("--apply", action="store_true",
                    help="實際寫入（不加此 flag 只 dry-run）")
    ap.add_argument("--deposit", type=float, default=DEFAULT_DEPOSIT,
                    help=f"每個 sector 加碼金額（預設 {DEFAULT_DEPOSIT:,.0f}）")
    ap.add_argument("--sectors", nargs="+", default=DEFAULT_SECTORS,
                    help=f"要處理的 sector（預設 {DEFAULT_SECTORS}）")
    args = ap.parse_args()

    print("=" * 60)
    print("加碼本金 + 補足 dust positions")
    print(f"  deposit: ${args.deposit:,.0f} / sector")
    print(f"  sectors: {args.sectors}")
    print(f"  mode:    {'APPLY (真的寫檔)' if args.apply else 'DRY-RUN (預覽)'}")
    print("=" * 60)

    summary = []
    for sector in args.sectors:
        result = process_account(sector, args.deposit, args.apply)
        summary.append(result)

    print("\n" + "=" * 60)
    print("總結")
    print("=" * 60)
    for r in summary:
        if r.get("skipped"):
            continue
        print(f"  {r['sector']}: 加碼 ${r['deposit']:,.0f}, dust 補足 {r['dust']} 筆 (${r['topup_cost']:,.0f})")

    if not args.apply:
        print("\n💡 確認無誤後加 --apply 真正執行：")
        print(f"   python3 scripts/recapitalize_and_cleanup_dust.py --apply")
    else:
        print("\n✅ 完成。記得重啟服務讓 RiskGate 用新的 initial_balance：")
        print("   launchctl unload ~/Library/LaunchAgents/local.crypto-signal-pro.plist")
        print("   launchctl load -w ~/Library/LaunchAgents/local.crypto-signal-pro.plist")


if __name__ == "__main__":
    main()
