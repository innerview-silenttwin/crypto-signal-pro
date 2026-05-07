#!/usr/bin/env python3
"""sinopac_login_check.py — 驗證 Shioaji credentials 與帳戶可用性。

僅執行：登入 → 列出帳戶 → 登出。**不下單、不啟用 CA**。

用法（prod 機）：
    python3 scripts/sinopac_login_check.py
    # 須先 source .env 或在 systemd 透過 EnvironmentFile 載入

期待輸出：
    ✅ Shioaji 登入成功（simulation=true）
    📒 stock account: ...
    📒 future account: ...

故障排除：
  - "ImportError: shioaji" → pip install shioaji
  - "login failed" → 檢查 SHIOAJI_API_KEY/SECRET_KEY 是否正確；簽過風險預告書
  - "no stock_account" → 確認永豐已開通 API 測試權限
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def _redact(s: str, keep: int = 4) -> str:
    if not s:
        return "(empty)"
    return f"{s[:keep]}***({len(s)} chars)"


def main() -> int:
    api_key = os.environ.get("SHIOAJI_API_KEY", "").strip()
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "").strip()
    person_id = os.environ.get("SHIOAJI_PERSON_ID", "").strip()
    raw_sim = os.environ.get("SHIOAJI_SIMULATION", "true")
    simulation = raw_sim.strip() != "false"

    print("── Shioaji credentials check ──")
    print(f"  SHIOAJI_API_KEY:    {_redact(api_key)}")
    print(f"  SHIOAJI_SECRET_KEY: {_redact(secret_key)}")
    print(f"  SHIOAJI_PERSON_ID:  {_redact(person_id, 1)}")
    print(f"  SHIOAJI_SIMULATION: {simulation}")
    print()

    if not (api_key and secret_key and person_id):
        print("❌ 缺少必要環境變數。請確認 .env 已載入。")
        return 2

    try:
        import shioaji as sj
    except ImportError:
        print("❌ shioaji 未安裝。執行：pip install shioaji")
        return 3

    print(f"➡️  連線 Shioaji（simulation={simulation}）...")
    api = sj.Shioaji(simulation=simulation)
    try:
        accounts = api.login(api_key=api_key, secret_key=secret_key)
    except Exception as e:
        print(f"❌ 登入失敗：{e.__class__.__name__}")
        return 4

    print(f"✅ 登入成功，回傳 {len(accounts) if accounts else 0} 個帳戶")

    stock_acc = getattr(api, "stock_account", None)
    fut_acc = getattr(api, "futopt_account", None)
    print(f"📒 stock_account:  {stock_acc}")
    print(f"📒 futopt_account: {fut_acc}")

    if simulation:
        print()
        print("ℹ️  目前是 simulation 模式。正式上線（v2）需：")
        print("   1. SHIOAJI_SIMULATION=false")
        print("   2. SHIOAJI_CA_PATH + SHIOAJI_CA_PASSWORD")
        print("   3. 程式內呼叫 activate_ca")

    try:
        api.logout()
    except Exception:
        pass

    print()
    print("✅ 檢查完成 — credentials 可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
