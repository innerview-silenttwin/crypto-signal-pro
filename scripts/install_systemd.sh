#!/bin/bash
#
# install_systemd.sh — 在 prod 機（Linux + systemd）安裝 crypto-signal-pro service
#
# 安全設計：
#   1. .env 放在 /etc/crypto-signal-pro/.env，由 systemd 用 EnvironmentFile= 載入
#      （而非 unit 檔內 Environment=…，避免 systemctl cat 直接洩漏）
#   2. 該檔權限 root:trader 0640，只有 root 與 trader 群組能讀
#   3. service 以非 root 用戶 trader 執行
#
# 使用方式（在 prod 機 root 執行）：
#   sudo bash scripts/install_systemd.sh /home/trader/crypto-signal-pro trader
#
# 參數：
#   $1  = 程式根目錄（預設 /opt/crypto-signal-pro）
#   $2  = 執行用戶（預設 trader）

set -euo pipefail

APP_DIR="${1:-/opt/crypto-signal-pro}"
RUN_USER="${2:-trader}"
ETC_DIR="/etc/crypto-signal-pro"
ENV_FILE="${ETC_DIR}/.env"
UNIT_FILE="/etc/systemd/system/crypto-signal-pro.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "請用 sudo / root 執行此腳本"
  exit 1
fi

if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  echo "用戶 $RUN_USER 不存在；先建立："
  echo "  sudo useradd -r -s /usr/sbin/nologin -d $APP_DIR $RUN_USER"
  exit 2
fi

if [ ! -d "$APP_DIR" ]; then
  echo "找不到程式目錄：$APP_DIR"
  exit 3
fi

# ── 1. /etc/crypto-signal-pro/.env ──
mkdir -p "$ETC_DIR"
chown root:"$RUN_USER" "$ETC_DIR"
chmod 0750 "$ETC_DIR"

if [ ! -f "$ENV_FILE" ]; then
  echo "→ 建立空的 $ENV_FILE（需手動填入）"
  cp "$APP_DIR/.env.example" "$ENV_FILE"
fi
chown root:"$RUN_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

# ── 2. systemd unit ──
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=crypto-signal-pro (FastAPI + sector auto-trader)
After=network-online.target
Wants=network-online.target
# 防 crash loop 撞 Shioaji 每天 1000 次登入上限：
# 5 分鐘內最多重啟 5 次，超過就讓 service 進 failed 狀態，等人工介入
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
# 確保時區固定（Asia/Taipei），不依賴系統 TZ
Environment=TZ=Asia/Taipei
ExecStart=/usr/bin/env python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
# 安全強化（systemd ≥ 232）
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${APP_DIR}/data ${APP_DIR}/backend/data
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true

[Install]
WantedBy=multi-user.target
EOF

chmod 0644 "$UNIT_FILE"

# ── 3. reload + enable ──
systemctl daemon-reload
systemctl enable crypto-signal-pro.service

cat <<EOF

✅ systemd unit 已安裝：${UNIT_FILE}
   .env 路徑：${ENV_FILE}（請用 root 編輯，並填入 SHIOAJI_* 等敏感變數）

下一步：
  1. sudo \$EDITOR ${ENV_FILE}     # 填入 SHIOAJI_API_KEY/SECRET_KEY/PERSON_ID
  2. sudo -u ${RUN_USER} python3 ${APP_DIR}/scripts/sinopac_login_check.py
  3. sudo systemctl start crypto-signal-pro
  4. sudo journalctl -u crypto-signal-pro -f

⚠️  確認過了再執行 systemctl start。下單前請先看 docs/sinopac_setup.md 的上線 checklist。
EOF
