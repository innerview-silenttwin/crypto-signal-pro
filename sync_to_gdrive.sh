#!/bin/bash
# 同步專案到 Google Drive（排除不需要的檔案）

SRC="/Users/ken_tsai/Documents/kentsai/crypto-signal-pro"
DEST="$HOME/Library/CloudStorage/GoogleDrive-ironsien007@gmail.com/我的雲端硬碟/crypto-signal-pro"

# Step 1：同步程式碼（排除 data/ 大型快取）
rsync -av --delete \
  --exclude='.git/' \
  --exclude='.pytest_cache/' \
  --exclude='node_modules/' \
  --exclude='__pycache__/' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='.DS_Store' \
  --exclude='.vscode/' \
  --exclude='.claude/settings.local.json' \
  --exclude='data/' \
  "$SRC/" "$DEST/"

# Step 2：單獨同步交易帳戶 JSON（排除 BTC CSV 快取）
rsync -av \
  --include='sector_accounts/' \
  --include='sector_accounts/*.json' \
  --include='btc_trading_account.json' \
  --exclude='*' \
  "$SRC/data/" "$DEST/data/"

echo "✅ 同步完成: $(date)"
