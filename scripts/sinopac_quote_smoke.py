"""Smoke test：驗證 SinopacQuoteProvider 能在 production 主機 + 無 CA 條件下拿 quote。

執行：
  cd ~/Documents/plate/crypto-signal-pro   # mini
  ./venv/bin/python scripts/sinopac_quote_smoke.py

預期：
  - 登入成功（無 CA warning 是正常）
  - 2330.TW 1d / 1m 都有資料
  - 物理上下單功能不暴露（呼叫 .place_order 應該找不到 method）
"""
import os
import sys

# 讓 backend.* 找得到（同 backend/main.py 的做法）
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

# 強制走 sinopac
os.environ["QUOTE_SOURCE"] = "sinopac"

from quote_provider import get_quote_provider
from quote_provider.factory import reset_quote_provider

reset_quote_provider()  # 確保拿到 sinopac 而非快取的 yfinance


def main():
    p = get_quote_provider()
    print(f"provider.name = {p.name}")
    assert p.name == "sinopac", f"expected sinopac, got {p.name}"

    # 1. 日線
    df_d = p.get_history("2330.TW", period_days=10, interval="1d")
    if df_d is None or df_d.empty:
        print("❌ 1d 2330.TW 無資料")
        sys.exit(1)
    print(f"✅ 2330.TW 1d: {len(df_d)} 筆，last close={df_d['close'].iloc[-1]:.2f} @ {df_d.index[-1]}")

    # 2. 1m
    df_m = p.get_history("2330.TW", period_days=1, interval="1m")
    if df_m is None or df_m.empty:
        print("⚠️  1m 2330.TW 無資料（盤後可能正常）")
    else:
        print(f"✅ 2330.TW 1m: {len(df_m)} 筆，last close={df_m['close'].iloc[-1]:.2f} @ {df_m.index[-1]}")

    # 3. 跨類股
    for sym in ["2454.TW", "6669.TW", "3443.TW"]:
        d = p.get_history(sym, period_days=5, interval="1d")
        ok = "✅" if (d is not None and not d.empty) else "❌"
        last = f"{d['close'].iloc[-1]:.2f}" if (d is not None and not d.empty) else "-"
        print(f"  {ok} {sym} 1d last={last}")

    # 4. Defense-in-depth：確認 provider 沒暴露 .place_order / .submit
    public_attrs = [a for a in dir(p) if not a.startswith("_")]
    print(f"\n📋 Provider 公開介面：{public_attrs}")
    if hasattr(p, "place_order"):
        print("🚨 SECURITY: provider exposes place_order!")
        sys.exit(2)
    if hasattr(p, "submit"):
        print("🚨 SECURITY: provider exposes submit!")
        sys.exit(2)
    print("✅ Defense-in-depth L1：provider 無 place_order / submit method")

    # 5. Defense-in-depth L2：確認 __api 走 name mangling，意外觸發成本高
    assert not hasattr(p, "_api"), "_api 不應該存在（已 mangled）"
    mangled_api = getattr(p, "_SinopacQuoteProvider__api", None)
    if mangled_api is None:
        print("⚠️  找不到 mangled __api（內部結構變動？）")
    else:
        # 提醒：mangled 後仍能拿到，但寫起來明顯帶警告意圖
        assert hasattr(mangled_api, "place_order"), "Shioaji 本身應該有 place_order（這是測試底層的）"
        print("✅ Defense-in-depth L2：__api 走 name mangling，需顯式寫 _SinopacQuoteProvider__api 才能拿到")

    print("\n=== ALL CHECKS PASSED ===")


if __name__ == "__main__":
    main()
