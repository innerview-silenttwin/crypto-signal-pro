#!/bin/bash
# 將關鍵 state 檔備份到 Google Drive（使用 ditto 避免大檔 mmap 問題）
#
# 備份範圍：
# - data/btc_trading_account.json     幣圈交易帳戶（持倉/現金/歷史）
# - data/sector_accounts/             台股各產業交易帳戶
# - backend/data/screener_rank_history.json  超選排名歷史
# - backend/data/backtest/            回測中心結果
#
# 使用方式：
#   手動：bash scripts/backup_state.sh
#   排程：crontab -e 加入：
#     0 * * * * /Users/ken_tsai/Documents/kentsai/crypto-signal-pro/scripts/backup_state.sh

set -u

SRC="/Users/ken_tsai/Documents/kentsai/crypto-signal-pro"
GDRIVE="$HOME/Google Drive/My Drive/crypto-signal-pro/backup"
LOG="$GDRIVE/backup.log"

mkdir -p "$GDRIVE"

TS=$(date '+%Y-%m-%d %H:%M:%S')

backup_file() {
  local rel="$1"
  if [[ -f "$SRC/$rel" ]]; then
    if ditto "$SRC/$rel" "$GDRIVE/$rel" 2>/dev/null; then
      echo "[$TS] OK $rel" >> "$LOG"
    else
      echo "[$TS] FAIL $rel" >> "$LOG"
    fi
  fi
}

backup_dir() {
  local rel="$1"
  if [[ -d "$SRC/$rel" ]]; then
    if ditto "$SRC/$rel" "$GDRIVE/$rel" 2>/dev/null; then
      echo "[$TS] OK $rel/" >> "$LOG"
    else
      echo "[$TS] FAIL $rel/" >> "$LOG"
    fi
  fi
}

# 單檔
backup_file "data/btc_trading_account.json"
backup_file "backend/data/screener_rank_history.json"

# 目錄
backup_dir "data/sector_accounts"
backup_dir "backend/data/backtest"

echo "[$TS] backup completed" >> "$LOG"
