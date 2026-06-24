#!/bin/bash
# 同步專案到 Google Drive（排除不需要的檔案）

SRC="/Users/ken_tsai/Documents/kentsai/crypto-signal-pro"
DEST="$HOME/Library/CloudStorage/GoogleDrive-ironsien007@gmail.com/我的雲端硬碟/crypto-signal-pro"

# Step 0：防衛性清掉 GDrive 上可能殘留的敏感檔 / 垃圾。
# 為何需要：rsync --delete 不會刪「被 exclude」的檔，所以敏感檔一旦曾被手動 copy
# 上去就會永久殘留（2026-06-24 發現 5/11 的 .env 含 broker key/token/身分證殘留 44 天）。
# ⚠️ 不用 rsync --delete-excluded：那會連 data/（帳本離線備份）一起砍，若 Step 2
#    重建前中斷就丟備份。改用明確 rm 只清「確定該清」的東西，data/ 永不受影響。
for junk in ".env" ".git" "shioaji.log" ".claude/settings.local.json" ".claude/worktrees" "__pycache__" ".pytest_cache" ".vscode"; do
  rm -rf "$DEST/$junk" 2>/dev/null
done
# .env.*（如 .env.bak）但保留 .env.example
find "$DEST" -maxdepth 1 -name '.env.*' ! -name '.env.example' -delete 2>/dev/null
find "$DEST" -name '*.log' -type f -delete 2>/dev/null

# Step 1：同步程式碼（plain --delete，不用 --delete-excluded → 絕不誤刪 data/）
# --include='.env.example' 必須放在 --exclude='.env.*' 前（rsync filter 依序 match）
rsync -av --delete \
  --include='.env.example' \
  --exclude='.env' \
  --exclude='.env.*' \
  --exclude='.git/' \
  --exclude='.pytest_cache/' \
  --exclude='node_modules/' \
  --exclude='__pycache__/' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='.DS_Store' \
  --exclude='.vscode/' \
  --exclude='.claude/settings.local.json' \
  --exclude='.claude/worktrees/' \
  --exclude='shioaji.log' \
  --exclude='*.log' \
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
