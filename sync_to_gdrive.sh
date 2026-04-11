#!/bin/bash
# 同步專案到 Google Drive（排除不需要的檔案）

DEST="$HOME/Library/CloudStorage/GoogleDrive-ironsien007@gmail.com/我的雲端硬碟/crypto-signal-pro/"

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
  \
  /Users/ken_tsai/Documents/kentsai/crypto-signal-pro/ \
  "$DEST"

echo "✅ 同步完成: $(date)"
