# 永豐證券 Shioaji 自動交易設置（Phase 1：simulation only）

本文件說明從零到上線的完整流程。**Phase 1 只跑 simulation**，不打真錢、不啟用 CA 憑證。

---

## 0. 安全前提（先看完再動手）

- 本系統的 `.env`、`broker_state.json`、`broker_config.yaml`、`cert/` 全部 gitignored，**永不入版本控制**。
- prod 機的 `.env` 透過 systemd `EnvironmentFile=` 載入；不要用 `Environment=` 把 key 塞進 unit 檔（`systemctl cat` 會看到）。
- prod 機建議獨立 SSH key 與 GitHub deploy key，**不要**共用個人 / 公司帳號的 token。
- Telegram 通知 chat 必須是私人對話（不要拉同事進去）。
- CA `.pfx` 的密碼不要寫在 commit message、log、Telegram；只放在 `.env`。

---

## 1. 申請永豐 API Key（用戶端）

依 https://ai.sinotrade.com.tw/python/Main/index.aspx：

1. **線上開戶** — 開立永豐證券帳戶（已開戶可跳過）
2. **線上申請 API Key**
   - 在永豐 e-leader 簽署中心簽署「API 電子交易風險預告書暨使用同意書」
   - 申請 API Key 與 Secret Key（永豐會提供 2 組字串）
3. **API 測試**
   - 在模擬環境（營業日 8am–8pm）跑 ≥ 1 週
   - 通過審核後永豐會開通正式下單權限
4. **CA 憑證**（v1 不啟用，先跳過）
   - v2 上正式才需下載 `.pfx`，密碼存好

完成後手上應有：
- `SHIOAJI_API_KEY`
- `SHIOAJI_SECRET_KEY`
- `SHIOAJI_PERSON_ID`（你的身分證字號，simulation 也需要）

---

## 2. prod 機部署

### 2.1 環境準備

```bash
# Linux + Python 3.10+
sudo apt update
sudo apt install -y python3 python3-pip git chrony

# NTP 校時（重要 — ROD 訂單對 ±2s 內敏感）
sudo systemctl enable --now chrony

# 建立執行用戶
sudo useradd -r -s /usr/sbin/nologin -d /opt/crypto-signal-pro trader
```

### 2.2 取得程式碼

```bash
# 用獨立的 deploy key（與 dev 機 GitHub key 區隔）
sudo -u trader git clone git@github.com:ken-tsai-prod/crypto-signal-pro.git /opt/crypto-signal-pro
cd /opt/crypto-signal-pro

sudo -u trader pip3 install --user -r backend/requirements.txt
sudo -u trader pip3 install --user shioaji
```

### 2.3 安裝 systemd service

```bash
sudo bash scripts/install_systemd.sh /opt/crypto-signal-pro trader
```

這個腳本會：
- 建 `/etc/crypto-signal-pro/.env`（root:trader 0640）
- 建 `/etc/systemd/system/crypto-signal-pro.service`
- `daemon-reload` + `enable`（**不**會自動 start）

### 2.4 填寫 `.env`

```bash
sudo $EDITOR /etc/crypto-signal-pro/.env
```

關鍵變數：

```env
BROKER_MODE=sinopac                    # prod 機才設成 sinopac
SHIOAJI_SIMULATION=true                # Phase 1 必須維持 true
SHIOAJI_API_KEY=<從永豐拿到>
SHIOAJI_SECRET_KEY=<從永豐拿到>
SHIOAJI_PERSON_ID=<身分證字號>
ALLOWED_SECTORS=semiconductor,electronics
TELEGRAM_BOT_TOKEN=<你個人的 bot>
TELEGRAM_CHAT_ID=<你個人 chat_id>
TZ=Asia/Taipei
```

> ⚠️ 任何 `SHIOAJI_SIMULATION` 寫成非 `false` 字串都會被 factory 當 `true` 處理（defense-in-depth）。
> 想關掉 simulation 必須**完全小寫的 `false`**，且同時提供 CA path/password。

### 2.5 複製風控配置

```bash
sudo -u trader cp data/broker_config.example.yaml data/broker_config.yaml
sudo -u trader $EDITOR data/broker_config.yaml
```

調整風控值：每筆金額上限、每日筆數、kill-switch 閾值、cooldown 時間等。

### 2.6 複製除權息與節假日行事曆

```bash
sudo -u trader cp data/ex_dividend_calendar.example.yaml data/ex_dividend_calendar.yaml
sudo -u trader cp data/tw_holidays.example.yaml data/tw_holidays.yaml
sudo -u trader $EDITOR data/ex_dividend_calendar.yaml   # 填白名單股票最近 3 個月除權息日
sudo -u trader $EDITOR data/tw_holidays.yaml             # 從 TWSE 行事曆抄當年休市日
```

> 維護週期：每月 1 日更新 `ex_dividend_calendar.yaml`；每年 1 月初更新 `tw_holidays.yaml`。
> 忘了更新 → 除權息日當天會觸發假停損；節假日當天會嘗試下單被券商拒絕。

### 2.7 驗證 credentials

```bash
sudo -u trader env $(grep -v '^#' /etc/crypto-signal-pro/.env | xargs) \
  python3 /opt/crypto-signal-pro/scripts/sinopac_login_check.py
```

期待輸出：
```
✅ Shioaji 登入成功（simulation=True）
📒 stock_account: ...
```

失敗時的常見原因：
- `ImportError: shioaji` → `pip install shioaji`
- `login failed` → 檢查 API_KEY/SECRET_KEY；確認永豐已開通簽核
- `no stock_account` → 確認永豐 API 測試權限已開通

### 2.8 啟動 service

```bash
sudo systemctl start crypto-signal-pro
sudo journalctl -u crypto-signal-pro -f
```

預期 log（前 30 秒）：
```
[sector_auto_trader] sector semiconductor broker=sinopac
[sector_auto_trader] sector electronics broker=sinopac
[sector_auto_trader] sector finance broker=virtual
🚀 類股自動交易已啟動 (間隔: 300秒)
```

---

## 3. 上線 checklist

部署完成且 service 跑起來後，**所有勾完才能放著走**：

- [ ] `cat /etc/crypto-signal-pro/.env | grep SHIOAJI_SIMULATION` → 確認是 `true`
- [ ] `cat /etc/crypto-signal-pro/.env | grep BROKER_MODE` → 確認是 `sinopac`
- [ ] `ls -l /etc/crypto-signal-pro/.env` → 權限 `-rw-r-----`，owner `root:trader`
- [ ] `systemctl cat crypto-signal-pro | grep -i shioaji` → **不應有任何 SHIOAJI_* 出現**（敏感變數應只在 .env）
- [ ] `git status -s` → 沒有任何 `data/broker_*.yaml`、`*.pfx`、`.env` 列出來
- [ ] `git ls-files | grep -E '\.env$|broker_config\.yaml$|broker_state\.json$|\.pfx$'` → 應該空白
- [ ] `python3 scripts/sinopac_login_check.py` → ✅ 登入成功
- [ ] 跑一輪 `_run_once`（手動：`python3 -c "from sector_auto_trader import auto_trader; auto_trader.run_once_now()"`）→ Telegram 應該收到一筆狀態通知（如有信號）
- [ ] 故意調 `max_daily_orders_total=1` 觀察第 2 筆是否被擋 + Telegram 警報
- [ ] `data/skipped_trades.jsonl` 不存在或為空（首次跑）
- [ ] `chrony` 校時誤差 < 2 秒：`chronyc tracking | grep "System time"`
- [ ] prod 機 Telegram chat 是私人對話，不是群組

---

## 4. 第一週每日監控

每天收盤後（14:30 後）跑一次：

```bash
# 看當日 skipped 與被擋的單
tail -n 50 /opt/crypto-signal-pro/data/skipped_trades.jsonl | python3 -m json.tool

# 看當日委託計數 & kill-switch
python3 -c "
import json
with open('/opt/crypto-signal-pro/data/broker_state.json') as f:
    s = json.load(f)
print('daily_lock:', s.get('daily_lock'))
print('today orders:', s.get('daily_orders', {}))
print('today realized PnL:', s.get('daily_realized_pnl', {}))
"

# 看實際 ledger 變化
diff <(jq -S .holdings /opt/crypto-signal-pro/data/sector_accounts/semiconductor_account.json) \
     <(ssh dev_mac jq -S .holdings ~/.../semiconductor_account.json) || true
```

異常徵兆：
- `daily_lock.active == true` 在收盤後仍未解 → **隔天先別重啟**，先檢查觸發原因
- `skipped_trades.jsonl` 全是 `below_min_lot` → 高價股無法下單；考慮提高 sector 資金或調整 ratio
- `pending_orders` 非空且超過 1 小時 → reconcile 異常，看 `journalctl` 找錯誤
- 同一 symbol 連續被買賣 → cooldown 沒生效，馬上停 service：`sudo systemctl stop crypto-signal-pro`

---

## 5. 切換到正式環境（v2，不在當前範圍）

當 simulation 至少跑滿 4 週、無任何 race condition / 風控失效，才考慮：

1. 從永豐取得 CA `.pfx` 與密碼
2. 把 `.pfx` 放在 `/opt/crypto-signal-pro/data/cert/`（owner trader, mode 0400）
3. 編輯 `.env`：
   ```env
   SHIOAJI_SIMULATION=false
   SHIOAJI_CA_PATH=/opt/crypto-signal-pro/data/cert/Sinopac.pfx
   SHIOAJI_CA_PASSWORD=<CA 密碼>
   ```
4. 重新跑 `scripts/sinopac_login_check.py` 確認 `activate_ca` 成功
5. **首日真實下單前**：把 `max_order_amount_twd` 暫時調到 ≤ 5000，跑一筆極小單
6. 確認 ledger、Telegram、reconcile 全對 → 才放回正常風控值

---

## 6. 故障排除

### service 起不來
```bash
sudo journalctl -u crypto-signal-pro -n 100 --no-pager
```

常見：
- `ModuleNotFoundError: shioaji` → `sudo -u trader pip3 install --user shioaji`
- `Permission denied` 對 `/etc/crypto-signal-pro/.env` → 檢查 owner / mode

### 下單拋例外
看 `journalctl` 找 `place_order failed:` 行：
- `contract_not_found:XXXX.TW` → symbol 在 Shioaji Contracts 找不到；確認代號正確
- `place_order_error` → 可能是參數問題或行情系統暫時不可用，自動會跳到 `skipped_trades.jsonl`

### 想暫停所有交易
```bash
sudo systemctl stop crypto-signal-pro
# 或不停 service，只暫停某 sector：
curl -X POST http://localhost:8000/api/sector-trading/semiconductor/toggle?active=false
```

### 想撤掉所有 in-flight 訂單
```bash
sudo systemctl stop crypto-signal-pro
python3 -c "
import json
with open('/opt/crypto-signal-pro/data/broker_state.json', 'r') as f:
    s = json.load(f)
print('pending:', s.get('pending_orders'))
"
# 如果有 pending：先到永豐 e-leader 手動撤單，再清空 pending_orders 後重啟
```

---

## 7. 程式架構參考

- 設計與決策：[backtest_system_spec.md](backtest_system_spec.md)
- Broker 抽象：[base.py](../backend/brokers/base.py)
- Risk 規則：[risk_gate.py](../backend/brokers/risk_gate.py)
- 市場時段：[market_hours.py](../backend/brokers/market_hours.py)
- Sinopac 包裝：[sinopac.py](../backend/brokers/sinopac.py)
- 整合測試：[tests/test_brokers/](../tests/test_brokers/)
