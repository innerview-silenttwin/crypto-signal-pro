#!/usr/bin/env python3
"""
一次性遷移腳本：合併 telecom 帳戶到 electronics + traditional
- 南電(8046)、可成(2474) 持倉+歷史 → electronics
- 台灣大(3045) 歷史 → traditional
- telecom 初始資金按持倉成本分配
"""
import json
import os
import shutil
from datetime import datetime

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sector_accounts")

# 搬往 electronics 的股票（含持倉）
TO_ELECTRONICS = {"8046.TW", "2474.TW"}
# 搬往 traditional 的股票（含已完成交易）
TO_TRADITIONAL = {"3045.TW", "2412.TW", "4904.TW", "2912.TW", "1590.TW", "2301.TW", "2049.TW", "1513.TW"}


def load(name):
    path = os.path.join(BASE, f"{name}_account.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save(name, data):
    path = os.path.join(BASE, f"{name}_account.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    telecom = load("telecom")
    electronics = load("electronics")
    traditional = load("traditional")

    print("=== 遷移前狀態 ===")
    for name, acct in [("telecom", telecom), ("electronics", electronics), ("traditional", traditional)]:
        print(f"  {name}: balance={acct['balance']:,.0f}, initial={acct['initial_balance']:,.0f}, "
              f"holdings={list(acct['holdings'].keys())}, history={len(acct['history'])}筆")

    # ── 計算 telecom 持倉總成本（歸屬 electronics 的部分）──
    electronics_cost = 0
    for sym in TO_ELECTRONICS:
        if sym in telecom["holdings"]:
            h = telecom["holdings"][sym]
            electronics_cost += h.get("total_cost", h["qty"] * h["avg_price"])

    traditional_capital = telecom["initial_balance"] - electronics_cost
    telecom_cash = telecom["balance"]

    print(f"\n=== 資金分配 ===")
    print(f"  telecom 初始: {telecom['initial_balance']:,.0f}")
    print(f"  → electronics 持倉成本: {electronics_cost:,.1f}")
    print(f"  → traditional 剩餘資本: {traditional_capital:,.1f}")
    print(f"  telecom 現金餘額: {telecom_cash:,.0f} → traditional")

    # ── 搬移 holdings ──
    for sym in list(TO_ELECTRONICS):
        if sym in telecom["holdings"]:
            print(f"\n  搬移持倉 {sym} → electronics")
            electronics["holdings"][sym] = telecom["holdings"].pop(sym)

    # ── 搬移 history ──
    elec_history_add = []
    trad_history_add = []
    for h in telecom["history"]:
        sym = h["symbol"]
        if sym in TO_ELECTRONICS:
            elec_history_add.append(h)
            print(f"  搬移歷史 {h['time']} {h['type']} {sym} → electronics")
        else:
            trad_history_add.append(h)
            print(f"  搬移歷史 {h['time']} {h['type']} {sym} → traditional")

    # 合併歷史（按時間排序）
    electronics["history"] = sorted(
        electronics["history"] + elec_history_add,
        key=lambda x: x["time"], reverse=True
    )
    traditional["history"] = sorted(
        traditional["history"] + trad_history_add,
        key=lambda x: x["time"], reverse=True
    )

    # ── 調整資金 ──
    # electronics: 加入持倉但不加現金（現金已花在持倉上），只增加 initial_balance
    electronics["initial_balance"] += electronics_cost
    # traditional: 加入 telecom 所有剩餘現金 + 對應 initial_balance
    traditional["balance"] += telecom_cash
    traditional["initial_balance"] += traditional_capital

    # ── 更新 sector metadata ──
    traditional["sector_name"] = "傳產/航運/電信"

    print(f"\n=== 遷移後狀態 ===")
    for name, acct in [("electronics", electronics), ("traditional", traditional)]:
        equity = acct["balance"]
        for h in acct["holdings"].values():
            equity += h.get("qty", 0) * h.get("avg_price", 0)
        print(f"  {name}: balance={acct['balance']:,.0f}, initial={acct['initial_balance']:,.0f}, "
              f"holdings={list(acct['holdings'].keys())}, history={len(acct['history'])}筆, "
              f"equity(估)={equity:,.0f}")

    # ── 備份 telecom 後儲存 ──
    backup_path = os.path.join(BASE, f"telecom_account.json.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(os.path.join(BASE, "telecom_account.json"), backup_path)
    print(f"\n  已備份 telecom → {os.path.basename(backup_path)}")

    save("electronics", electronics)
    save("traditional", traditional)

    # 移除 telecom 帳戶檔
    os.remove(os.path.join(BASE, "telecom_account.json"))
    print("  已刪除 telecom_account.json")

    print("\n✅ 遷移完成！")


if __name__ == "__main__":
    main()
