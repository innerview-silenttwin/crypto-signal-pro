#!/usr/bin/env python3
"""sync_ledger_to_sinopac.py — 同步系統 ledger 持倉到永豐 simulation 真實狀態

問題情境：
  - 我們 sector_accounts/*.json (ledger) 內有兩種持倉：
    1. 5/11 前 VirtualBroker 累積的（永豐主機**完全不知道**）
    2. 5/11 後 SinopacBroker 真的撮合成功的（永豐主機**有記錄**）
  - 5/13 跑過 dust topup 把零頭補到 10 股（只改 ledger、沒打永豐）
  - 結果：系統送 SELL 時數量超過永豐認可，永豐回 status.failed

解法：以**永豐 simulation 為準**，把 ledger 校正到一致。
  - 永豐有的：留下，數量對齊永豐
  - 永豐沒有的：從 ledger 移除，退錢給 balance
  - dust topup 加上去的（永豐 < ledger）：減到永豐數量，退差額給 balance
  - 永豐有但 ledger 沒有的：補進 ledger（理論上不會發生，但保險）

執行：
    # 預覽（dry-run）
    python3 scripts/sync_ledger_to_sinopac.py

    # 實際套用
    python3 scripts/sync_ledger_to_sinopac.py --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# ── .env 與 SSL ──
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = PROJECT_ROOT / "data" / "sector_accounts"
DEFAULT_SECTORS = ["semiconductor", "electronics"]
TW_FEE_RATE = 0.001425


def _info(m): print(f"  {m}")
def _ok(m): print(f"✅ {m}")
def _warn(m): print(f"⚠️  {m}")
def _fail(m, code=1):
    print(f"❌ {m}")
    sys.exit(code)


def atomic_write_json(path: Path, data: dict) -> None:
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


def fetch_sinopac_positions() -> dict[str, dict]:
    """從永豐 simulation 取得當前持倉（dict by code）"""
    api_key = os.environ.get("SHIOAJI_API_KEY", "").strip()
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "").strip()
    if not (api_key and secret_key):
        _fail("缺 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY；確認 .env 有設定")

    try:
        import shioaji as sj
    except ImportError:
        _fail("shioaji 未安裝；請在 venv 內跑 pip install shioaji", code=2)

    api = sj.Shioaji(simulation=True)
    try:
        api.login(api_key=api_key, secret_key=secret_key, fetch_contract=False)
    except Exception as e:
        _fail(f"login 失敗：{e.__class__.__name__}", code=3)

    positions = api.list_positions(api.stock_account)
    result = {}
    for p in (positions or []):
        code = str(getattr(p, "code", "")).strip()
        if not code:
            continue
        result[code] = {
            "qty": int(getattr(p, "quantity", 0) or 0),
            "avg_price": float(getattr(p, "price", 0.0) or 0.0),
        }
    return result


def normalize_symbol(sym: str) -> str:
    """ledger 用 '2330.TW' / '6223.TWO'；永豐用純 code '2330' / '6223'。回傳純 code。"""
    return sym.replace(".TWO", "").replace(".TW", "")


def process_sector(sector: str, sinopac_positions: dict, apply: bool) -> dict:
    path = ACCOUNTS_DIR / f"{sector}_account.json"
    if not path.exists():
        _warn(f"{sector}: 找不到 {path}")
        return {"sector": sector, "skipped": True}

    with open(path) as f:
        state = json.load(f)

    old_balance = float(state.get("balance", 0))
    holdings = state.get("holdings", {}) or {}

    print(f"\n── {sector} ──")
    _info(f"目前 balance: ${old_balance:,.0f}")
    _info(f"目前 ledger 持倉: {sum(1 for h in holdings.values() if h.get('qty', 0) > 0)} 檔")

    changes = []   # list of dict: {symbol, action, ledger_qty, sinopac_qty, refund, ...}
    new_balance = old_balance
    new_holdings = dict(holdings)

    # 1. ledger 中每筆持倉
    for sym, h in list(holdings.items()):
        ledger_qty = int(h.get("qty", 0) or 0)
        if ledger_qty <= 0:
            continue
        code = normalize_symbol(sym)
        avg_price = float(h.get("avg_price", 0) or 0)
        sinopac_qty = sinopac_positions.get(code, {}).get("qty", 0)

        if sinopac_qty == ledger_qty:
            continue  # 完美對齊，無動作

        if sinopac_qty > ledger_qty:
            # 永豐有更多（理論上不會，但保險）
            delta = sinopac_qty - ledger_qty
            cost_increase = delta * avg_price * (1 + TW_FEE_RATE)
            changes.append({
                "symbol": sym,
                "action": "increase",
                "ledger_qty_before": ledger_qty,
                "ledger_qty_after": sinopac_qty,
                "sinopac_qty": sinopac_qty,
                "delta_qty": delta,
                "balance_change": -cost_increase,
            })
            new_balance -= cost_increase
            new_holdings[sym] = {**h, "qty": sinopac_qty}
            continue

        # sinopac_qty < ledger_qty → 退錢 + 縮減
        delta_qty = ledger_qty - sinopac_qty
        # 退原成本（含手續費）回 balance
        refund = delta_qty * avg_price * (1 + TW_FEE_RATE)
        new_balance += refund

        if sinopac_qty == 0:
            # 完全 phantom，移除
            changes.append({
                "symbol": sym,
                "action": "remove",
                "ledger_qty_before": ledger_qty,
                "ledger_qty_after": 0,
                "sinopac_qty": 0,
                "delta_qty": delta_qty,
                "balance_change": refund,
                "avg_price": avg_price,
            })
            del new_holdings[sym]
        else:
            # 部分 phantom（dust topup 造成），縮減
            # total_cost 按比例縮減
            old_total_cost = float(h.get("total_cost", ledger_qty * avg_price * (1 + TW_FEE_RATE)))
            new_total_cost = round(old_total_cost * (sinopac_qty / ledger_qty), 2)
            changes.append({
                "symbol": sym,
                "action": "reduce",
                "ledger_qty_before": ledger_qty,
                "ledger_qty_after": sinopac_qty,
                "sinopac_qty": sinopac_qty,
                "delta_qty": delta_qty,
                "balance_change": refund,
                "avg_price": avg_price,
            })
            new_holdings[sym] = {
                **h,
                "qty": sinopac_qty,
                "total_cost": new_total_cost,
            }

    # 2. 永豐有但 ledger 沒有的（理論上不會發生，但保險檢查）
    ledger_codes = {normalize_symbol(s) for s, h in holdings.items() if (h.get("qty", 0) or 0) > 0}
    for code, sp in sinopac_positions.items():
        if code in ledger_codes or sp["qty"] <= 0:
            continue
        # 兩種寫法都試（.TW / .TWO）— 預設用 .TW
        sym_candidate = f"{code}.TW"
        # 這個 sector 不應該有這檔 → 跳過、警告
        # （邏輯上每檔股票只屬於一個 sector，要跨 sector 處理太複雜）
        _warn(f"  {code}: 永豐有 {sp['qty']} 股但 ledger 沒有，可能在別的 sector，跳過")

    # 3. 印出變動
    if not changes:
        _info("無變動，ledger 與永豐已對齊 ✅")
        return {"sector": sector, "changes": 0}

    _info(f"{len(changes)} 筆變動：")
    print(f"  {'股票':<10}{'動作':<8}{'ledger':<10}{'→':<3}{'after':<10}{'sinopac':<10}{'退/扣金額':<12}")
    print(f"  {'-'*70}")
    total_refund = 0.0
    for c in changes:
        emoji = "🗑" if c["action"] == "remove" else ("✂" if c["action"] == "reduce" else "➕")
        bc = c["balance_change"]
        total_refund += bc
        print(f"  {c['symbol']:<10}{emoji} {c['action']:<6}{c['ledger_qty_before']:<10}→ {c['ledger_qty_after']:<10}{c['sinopac_qty']:<10}{bc:+,.0f}")
    print(f"  {'-'*70}")
    print(f"  總退錢 → balance: {total_refund:+,.0f}")
    print(f"  balance: ${old_balance:,.0f} → ${new_balance:,.0f}")

    if not apply:
        _info("(dry-run，未實際寫檔)")
        return {"sector": sector, "changes": len(changes), "refund": total_refund, "details": changes}

    # 4. apply：備份 + history + atomic write
    backup_path = path.with_suffix(f".json.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, backup_path)
    _ok(f"備份 → {backup_path.name}")

    # 寫 history：每筆變動加一筆 SYNC entry
    history = state.setdefault("history", [])
    now_ts = int(time.time() * 1000)
    for i, c in enumerate(changes):
        history.insert(0, {
            "id": now_ts + i,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": c["symbol"],
            "name": c["symbol"],   # 不查 stock_name table
            "type": "SYNC",
            "subtype": c["action"],
            "qty_before": c["ledger_qty_before"],
            "qty_after": c["ledger_qty_after"],
            "delta_qty": c["delta_qty"],
            "balance_change": round(c["balance_change"], 2),
            "signal": f"sync_to_sinopac: {c['action']} ({c['ledger_qty_before']}→{c['ledger_qty_after']})",
            "balance_after": round(new_balance, 2),
        })

    state["balance"] = round(new_balance, 2)
    state["holdings"] = new_holdings

    atomic_write_json(path, state)
    _ok(f"已套用 → {path.name}")

    return {"sector": sector, "changes": len(changes), "refund": total_refund}


def main():
    ap = argparse.ArgumentParser(description="同步 ledger 到永豐 simulation 持倉")
    ap.add_argument("--apply", action="store_true",
                    help="實際寫入（不加此 flag 只 dry-run）")
    ap.add_argument("--sectors", nargs="+", default=DEFAULT_SECTORS)
    args = ap.parse_args()

    print("=" * 70)
    print("同步 ledger → 永豐 simulation 持倉")
    print(f"  mode: {'APPLY (真的寫檔)' if args.apply else 'DRY-RUN (預覽)'}")
    print(f"  sectors: {args.sectors}")
    print("=" * 70)

    print("\n➡️  從永豐查目前持倉...")
    sinopac_positions = fetch_sinopac_positions()
    _ok(f"取得 {len(sinopac_positions)} 檔持倉")
    for code, sp in sorted(sinopac_positions.items()):
        _info(f"  {code}: {sp['qty']} 股 @${sp['avg_price']:,.2f}")

    summary = []
    for sector in args.sectors:
        r = process_sector(sector, sinopac_positions, args.apply)
        summary.append(r)

    print("\n" + "=" * 70)
    print("總結")
    print("=" * 70)
    for r in summary:
        if r.get("skipped"):
            print(f"  {r['sector']}: 跳過")
        elif r.get("changes", 0) == 0:
            print(f"  {r['sector']}: 已對齊，無變動 ✅")
        else:
            print(f"  {r['sector']}: {r['changes']} 筆變動，退錢 {r.get('refund', 0):+,.0f}")

    if not args.apply:
        print("\n💡 確認無誤後加 --apply 真正執行：")
        print(f"   python3 scripts/sync_ledger_to_sinopac.py --apply")
    else:
        print("\n✅ 完成。建議重啟服務讓設定生效：csp restart")


if __name__ == "__main__":
    main()
