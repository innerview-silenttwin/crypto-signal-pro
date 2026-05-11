#!/bin/bash
#
# install_launchd_mac.sh — 在 Mac mini（macOS）安裝 crypto-signal-pro 常駐服務
#
# 使用方式（在 Mac mini 上，不需 sudo）：
#   bash scripts/install_launchd_mac.sh
#
# 服務在登入後自動啟動、crash 後 10 秒自動重啟。
#
# 常用指令（裝完後）：
#   查狀態：launchctl list | grep crypto
#   停止  ：launchctl unload ~/Library/LaunchAgents/me.linego.crypto-signal-pro.plist
#   啟動  ：launchctl load -w ~/Library/LaunchAgents/me.linego.crypto-signal-pro.plist
#   日誌  ：tail -f ~/Documents/plate/crypto-signal-pro/logs/crypto-signal-pro.log

set -euo pipefail

APP_DIR="/Users/kentsai/Documents/plate/crypto-signal-pro"
PYTHON="${APP_DIR}/venv/bin/python3"
PLIST_ID="local.crypto-signal-pro"
PLIST_PATH="${HOME}/Library/LaunchAgents/${PLIST_ID}.plist"
LOG_DIR="${APP_DIR}/logs"

# ── 前置檢查 ──
if [ ! -f "$PYTHON" ]; then
  echo "❌ 找不到 Python：$PYTHON"
  echo "   請先建立 venv：cd $APP_DIR && python3 -m venv venv && venv/bin/pip install -r backend/requirements.txt"
  exit 1
fi

if [ ! -f "$APP_DIR/backend/main.py" ]; then
  echo "❌ 找不到主程式：$APP_DIR/backend/main.py"
  exit 2
fi

if [ ! -f "$APP_DIR/.env" ]; then
  echo "❌ 找不到 .env：$APP_DIR/.env"
  echo "   請先把 .env 放到 Mac mini 的專案目錄"
  exit 3
fi

mkdir -p "$LOG_DIR"
mkdir -p "${HOME}/Library/LaunchAgents"

# ── 寫 wrapper 腳本（launchd 不支援 EnvironmentFile，用 wrapper source .env）──
cat > "${APP_DIR}/scripts/run_prod.sh" <<WRAPPER
#!/bin/bash
# launchd wrapper：載入 .env 再啟動服務
set -a
source "${APP_DIR}/.env"
set +a

cd "${APP_DIR}"
exec "${PYTHON}" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1
WRAPPER
chmod +x "${APP_DIR}/scripts/run_prod.sh"

# ── 寫 plist ──
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_ID}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${APP_DIR}/scripts/run_prod.sh</string>
  </array>

  <!-- 登入後自動啟動 -->
  <key>RunAtLoad</key>
  <true/>

  <!-- crash 後自動重啟，throttle 10 秒防 crash loop -->
  <key>KeepAlive</key>
  <dict>
    <key>Crashed</key>
    <true/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>10</integer>

  <!-- log 輸出 -->
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/crypto-signal-pro.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/crypto-signal-pro-error.log</string>
</dict>
</plist>
PLIST

# ── 載入服務 ──
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"

echo ""
echo "✅ 服務已安裝並啟動"
echo ""
echo "查狀態：launchctl list | grep crypto"
echo "查日誌：tail -f ${LOG_DIR}/crypto-signal-pro.log"
echo "查錯誤：tail -f ${LOG_DIR}/crypto-signal-pro-error.log"
