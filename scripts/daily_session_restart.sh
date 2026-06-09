#!/usr/bin/env bash
# 每日 08:20 重啟 service，讓 Shioaji session 重新 login。
#
# 為什麼需要：永豐 Shioaji token 24 小時過期；常駐 daemon 跑超過 24h 後
# session 被 broker peer reset 就回不來（2026-06-08 incident）。
# 客服建議：重建整個 Shioaji() instance + 每日重登（見 project_sinopac_session_lifecycle）。
# 目前先用粗暴版「csp restart」，未來中期改為「broker 內部 instance 重建」(步驟 2B)。
#
# Time guard：mini 預設 01:30 睡 → 08:00 醒（pmset repeat schedule）。
# 若 mini 在 08:20 是睡眠，launchd 預設「醒來時補跑」可能落到交易時段。
# 我們不設 WakeFromSleep（讓 mini 睡）+ 此處 time guard 雙保險擋掉補跑。

set -e

HOUR=$(date +%H)
MIN=$(date +%M)
NOW="$(date '+%Y-%m-%d %H:%M:%S')"

# 允許窗口：08:00 ~ 08:50（給 macOS wake 後 5-10 分 buffer，且早於 08:30 premarket cron 留 buffer）
if [ "$HOUR" != "08" ]; then
    echo "[$NOW] skip: 不在 08 時"
    exit 0
fi
if [ "$MIN" -lt 0 ] || [ "$MIN" -gt 50 ]; then
    echo "[$NOW] skip: 時段 08:$MIN 不在 00-50 窗口（可能是補跑）"
    exit 0
fi

PROJECT_DIR="$HOME/Documents/plate/crypto-signal-pro"
if [ ! -d "$PROJECT_DIR" ]; then
    echo "[$NOW] FAIL: project dir not found: $PROJECT_DIR"
    exit 1
fi

echo "[$NOW] 開始每日 session restart"
cd "$PROJECT_DIR"
./scripts/csp.sh restart
echo "[$NOW] 完成"
