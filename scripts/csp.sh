#!/bin/bash
# csp — crypto-signal-pro 常用指令快捷工具
#
# 安裝（在 ~/.zshrc 加一行 alias）：
#   alias csp='bash ~/Documents/plate/crypto-signal-pro/scripts/csp.sh'
#   source ~/.zshrc
#
# 用法：
#   csp restart    # 重啟服務（unload + load + health）
#   csp health     # 健康檢查
#   csp logs       # 即時跟著看 log
#   csp errors     # 看錯誤 log（最後 30 行）
#   csp update     # git pull + 重啟服務
#   csp status     # 服務狀態 + 主要狀態摘要
#   csp help       # 顯示說明

APP_DIR="$HOME/Documents/plate/crypto-signal-pro"
PLIST="$HOME/Library/LaunchAgents/local.crypto-signal-pro.plist"

cmd="${1:-help}"

# 重啟時備份舊 log → health_check 數字 = 「自上次重啟以來」≈ 通常一天
rotate_logs() {
    local ts
    ts=$(date +%Y%m%d_%H%M%S)
    local logdir="$APP_DIR/logs"
    for f in crypto-signal-pro.log crypto-signal-pro-error.log; do
        if [ -s "$logdir/$f" ]; then
            mv "$logdir/$f" "$logdir/$f.$ts"
        fi
    done
    # 保留最近 7 份備份，其他刪掉避免堆積
    ls -t "$logdir"/crypto-signal-pro.log.* 2>/dev/null | tail -n +8 | xargs -r rm
    ls -t "$logdir"/crypto-signal-pro-error.log.* 2>/dev/null | tail -n +8 | xargs -r rm
}

case "$cmd" in
    restart)
        echo "→ 備份舊 log..."
        rotate_logs
        echo "→ 重啟服務..."
        launchctl unload "$PLIST" 2>/dev/null || true
        launchctl load -w "$PLIST"
        sleep 2
        echo "→ 健康檢查..."
        bash "$APP_DIR/scripts/health_check.sh"
        ;;
    health)
        bash "$APP_DIR/scripts/health_check.sh"
        ;;
    logs)
        echo "→ tail -f log（Ctrl+C 退出）"
        tail -f "$APP_DIR/logs/crypto-signal-pro.log"
        ;;
    errors)
        echo "→ 錯誤 log 最後 30 行"
        tail -30 "$APP_DIR/logs/crypto-signal-pro-error.log"
        ;;
    update)
        echo "→ git pull..."
        (cd "$APP_DIR" && git pull)
        echo "→ 備份舊 log..."
        rotate_logs
        echo "→ 重啟服務..."
        launchctl unload "$PLIST" 2>/dev/null || true
        launchctl load -w "$PLIST"
        sleep 2
        bash "$APP_DIR/scripts/health_check.sh"
        ;;
    status)
        launchctl list | grep crypto-signal-pro || echo "(服務沒在跑)"
        ;;
    help|*)
        echo "csp — crypto-signal-pro 快捷工具"
        echo ""
        echo "  csp restart    重啟服務並跑健康檢查"
        echo "  csp health     健康檢查"
        echo "  csp logs       即時跟著看 log（Ctrl+C 退出）"
        echo "  csp errors     錯誤 log 最後 30 行"
        echo "  csp update     git pull + 重啟服務"
        echo "  csp status     服務狀態"
        echo "  csp help       顯示此說明"
        ;;
esac
