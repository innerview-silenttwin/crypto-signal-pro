#!/usr/bin/env python3
"""sinopac_signed_test.py — 完成永豐 API 「測試」階段，拿到 signed=True

永豐簽署 API 服務時要求你必須在 simulation 環境跑通：
  1. 登入測試
  2. **CA 憑證啟用測試（activate_ca）— 即使 simulation 也要做**
  3. 股票模擬下單測試（買單）

跑通之後，永豐後台會把你的帳號標記為「測試完成」/`signed=True`，
然後人工審核 1-2 個工作日才會開通正式下單權限。

---

## 你怎麼用

### 前置條件
- Python 3.10+
- pip install shioaji
- 簽署過程中拿到的 SHIOAJI_API_KEY 與 SHIOAJI_SECRET_KEY
- 你的身分證字號 (SHIOAJI_PERSON_ID)
- **時間：營業日 8am – 8pm 台灣時間**（永豐 simulation 環境只在這時段開放）

### 執行方式

```bash
export SHIOAJI_API_KEY=<永豐給你的 key>
export SHIOAJI_SECRET_KEY=<永豐給你的 secret>
export SHIOAJI_PERSON_ID=<你身分證字號>
python3 scripts/sinopac_signed_test.py
```

或者一次性 inline：
```bash
SHIOAJI_API_KEY=xxx SHIOAJI_SECRET_KEY=yyy SHIOAJI_PERSON_ID=A123456789 \
  python3 scripts/sinopac_signed_test.py
```

### 預期輸出

每一步都印 ✅ 或 ❌；全綠表示測試已被永豐後台記錄。

### 跑完後到哪裡確認 signed=True

1. 登入永豐 e-leader / 簽署中心
2. API 申請進度頁應該看到「測試已完成」或類似字樣
3. 沒看到的話，1-2 小時後再 refresh，或聯絡永豐客服回報你跑了測試

---

## 安全說明

- 全程使用 `Shioaji(simulation=True)` — 不會打到正式環境、不會扣錢
- 下單價格刻意設為 100 元（遠低於台積電現價），確保不會成交
- 下單後立刻取消，狀態保持乾淨
- API_KEY / SECRET_KEY 不會被 log 出來，只顯示前 4 碼遮罩
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 自動載入 repo 根目錄的 .env（讓使用者不需要 export 變數）
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    # python-dotenv 未裝也沒關係 —— 退化成只讀 os.environ
    pass

# 修補 macOS Python.org 版的 SSL 憑證問題
# 預設 Python.org 不帶系統 CA bundle，會讓 shioaji 連 cloud config 端點時 SSL 驗證失敗
if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass

# 把 shioaji 內建 logger 降級，避免 INFO 訊息夾帶內部欄位
logging.getLogger("shioaji").setLevel(logging.WARNING)
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def _redact(s: str, keep: int = 4) -> str:
    if not s:
        return "(empty)"
    return f"{s[:keep]}***({len(s)} chars)"


def _fail(msg: str, code: int = 1) -> None:
    print(f"\n❌ {msg}")
    sys.exit(code)


def _ok(msg: str) -> None:
    print(f"✅ {msg}")


def _info(msg: str) -> None:
    print(f"   {msg}")


def main() -> int:
    print("=" * 60)
    print("永豐 Shioaji API 測試腳本（取得 signed=True）")
    print("=" * 60)

    # ── Step 0：環境檢查 ──
    api_key = os.environ.get("SHIOAJI_API_KEY", "").strip()
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "").strip()
    person_id = os.environ.get("SHIOAJI_PERSON_ID", "").strip()
    ca_path = os.environ.get("SHIOAJI_CA_PATH", "").strip()
    ca_password = os.environ.get("SHIOAJI_CA_PASSWORD", "").strip()

    print("\n── 環境變數檢查 ──")
    print(f"  SHIOAJI_API_KEY:    {_redact(api_key)}")
    print(f"  SHIOAJI_SECRET_KEY: {_redact(secret_key)}")
    print(f"  SHIOAJI_PERSON_ID:  {_redact(person_id, 1)}")
    print(f"  SHIOAJI_CA_PATH:    {ca_path or '(empty)'}")
    print(f"  SHIOAJI_CA_PASSWORD:{_redact(ca_password, 0)}")

    if not (api_key and secret_key and person_id):
        _fail("缺必要環境變數。請先 export SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY / SHIOAJI_PERSON_ID")
    if not (ca_path and ca_password):
        _fail("缺 CA 設定。永豐測試流程要求啟用 CA 才會標記 signed=True。\n"
              "   請先在永豐網站下載 .pfx 憑證、設定密碼，然後填入 .env 的 SHIOAJI_CA_PATH / SHIOAJI_CA_PASSWORD")
    if not os.path.exists(ca_path):
        _fail(f"CA 檔案不存在：{ca_path}\n   請確認路徑（macOS 下載通常在 ~/Downloads/）")

    # ── 時段檢查（提醒用，不強制中止）──
    now = datetime.now()
    weekday = now.weekday()  # 0=週一, 6=週日
    hour = now.hour
    if weekday >= 5:
        print("\n⚠️  目前是週末，永豐 simulation 環境**可能無法登入**（一般只在平日 8am-8pm 開放）")
        print("   若登入失敗，請改在週一至週五 8am-8pm 重跑")
    elif not (8 <= hour < 20):
        print(f"\n⚠️  目前 {hour}:xx，永豐 simulation 環境僅在 8am-8pm 開放")
        print("   若登入失敗，請在時段內重跑")

    # ── shioaji 套件 ──
    try:
        import shioaji as sj
    except ImportError:
        _fail("shioaji 未安裝。執行：pip install shioaji", code=2)

    print(f"\n── shioaji 版本：{getattr(sj, '__version__', 'unknown')} ──")

    # ── Step 1：登入 ──
    print("\n➡️  Step 1：登入（simulation=True）")
    api = sj.Shioaji(simulation=True)
    try:
        accounts = api.login(
            api_key=api_key,
            secret_key=secret_key,
            fetch_contract=False,    # 與官方範例對齊；下單前才取 contract
        )
    except Exception as e:
        _fail(f"登入失敗：{e.__class__.__name__}（檢查 KEY/SECRET 是否正確；簽過風險預告書）", code=3)

    _ok(f"登入成功，{len(accounts) if accounts else 0} 個帳戶")

    stock_acc = getattr(api, "stock_account", None)
    if not stock_acc:
        _fail("api.stock_account 不存在 — 你的帳號可能還沒開通 API 測試權限", code=4)

    _info(f"stock_account: {stock_acc}")
    fut_acc = getattr(api, "futopt_account", None)
    if fut_acc:
        _info(f"futopt_account: {fut_acc}（你沒申請期貨，本腳本不會測試期貨下單）")

    # ── Step 1.5：activate_ca（永豐測試必需）──
    print("\n➡️  Step 1.5：啟用 CA 憑證（activate_ca）")
    try:
        # 注意：1.3.x 預設不需 person_id 參數；若版本舊會丟 TypeError，再嘗試帶 person_id
        try:
            ok = api.activate_ca(ca_path=ca_path, ca_passwd=ca_password)
        except TypeError:
            ok = api.activate_ca(ca_path=ca_path, ca_passwd=ca_password, person_id=person_id)
    except Exception as e:
        _fail(f"activate_ca 失敗：{e.__class__.__name__}\n"
              f"   檢查：1) ca_path 是否正確、2) 密碼是否正確、3) 憑證是否已過期", code=5)

    if ok:
        _ok("CA 已啟用 → 永豐後台應認可此次測試")
    else:
        _fail("activate_ca 回傳 False（憑證或密碼可能不對）", code=5)

    # ── Step 2：取合約 ──
    print("\n➡️  Step 2：取得 2330 (台積電) 合約")
    try:
        # login 時 fetch_contract=False，這裡明確抓
        api.fetch_contracts(contract_download=True)
    except Exception as e:
        _info(f"fetch_contracts 警告（不致命）：{e.__class__.__name__}")

    try:
        contract = api.Contracts.Stocks["2330"]
    except (KeyError, AttributeError, Exception) as e:
        _fail(f"取合約失敗：{e.__class__.__name__}", code=6)

    contract_name = getattr(contract, "name", "2330")
    _ok(f"合約：{contract_name}")

    # ── Step 3：下模擬限價買單（限價 100，遠低於現價，不會成交）──
    print("\n➡️  Step 3：下模擬限價買單（1 張 @ 100 元，刻意不會成交）")
    try:
        buy_order = api.Order(
            action=sj.constant.Action.Buy,
            price=100.0,
            quantity=1,
            price_type=sj.constant.StockPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            order_lot=sj.constant.StockOrderLot.Common,
            order_cond=sj.constant.StockOrderCond.Cash,
            account=stock_acc,
        )
    except Exception as e:
        _fail(f"建構 Order 物件失敗：{e.__class__.__name__}", code=7)

    try:
        buy_trade = api.place_order(contract, buy_order)
    except Exception as e:
        _fail(f"place_order 失敗：{e.__class__.__name__}", code=8)

    order_id = ""
    try:
        order_id = getattr(buy_trade.status, "id", "") or getattr(buy_trade.order, "id", "")
    except Exception:
        pass
    _ok(f"買單已送出（order_id: {order_id or '(unknown)'}）")

    time.sleep(1.5)

    try:
        api.update_status()
    except Exception:
        # 部分版本要求 account 參數；這裡忽略，下一步直接取消
        pass

    status_str = ""
    try:
        status_str = str(getattr(buy_trade.status, "status", ""))
    except Exception:
        pass
    _info(f"目前狀態：{status_str or '(unknown)'}")

    # ── Step 4：取消買單 ──
    print("\n➡️  Step 4：取消買單（保持狀態乾淨）")
    try:
        api.cancel_order(buy_trade)
        time.sleep(1)
        try:
            api.update_status()
        except Exception:
            pass
        _ok("已送出取消")
    except Exception as e:
        # 取消失敗不算 fatal —— 永豐已記錄到下單動作
        print(f"⚠️  取消失敗（{e.__class__.__name__}）— 不影響測試完成判定，但建議到後台手動取消")

    # ── Step 5：登出 ──
    print("\n➡️  Step 5：登出")
    try:
        api.logout()
        _ok("登出完成")
    except Exception as e:
        print(f"⚠️  登出失敗（{e.__class__.__name__}）— 不影響測試結果")

    # ── 收尾 ──
    print("\n" + "=" * 60)
    print("✅ 測試流程跑完")
    print("=" * 60)
    print("\n接下來：")
    print("  1. 回到永豐 e-leader / API 申請進度頁")
    print("  2. 確認看到「測試已完成」或 signed=True")
    print("  3. 沒看到 → 等 1-2 小時 refresh；仍無 → 聯絡永豐客服回報你已跑測試")
    print("  4. 永豐審核（通常 1-2 個工作日）→ 開通正式下單權限")
    print()
    print("拿到 signed=True 之後，回來告訴我，我們進 prod 機部署。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
