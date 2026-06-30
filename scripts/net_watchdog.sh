#!/bin/bash
# 網路 watchdog（主動偵測 + 自動重連 Wi-Fi）
#
# 由 launchd 每 30 分鐘執行一次（local.crypto-net-watchdog.plist）。
# 機制：實際 ping 外網（測「封包真的通不通」、非看 wifi 狀態旗標）；連 3 次失敗
#       才判定斷線 → 自動把 Wi-Fi(en1) 關開重連（= 手動關開 wifi 的自動版）→ 復原後
#       打 localhost 內部端點發 Telegram 通知。
#
# 安全：本腳本不含任何密鑰；Telegram token 留在 app 的 .env、由 app 發送。
#       僅在「已復原(localhost 通)」時，從 .env 讀 CSP_INTERNAL_KEY 帶 header 打 localhost。
#       只動 en1(wifi)，不碰 en0(乙太/SSH 管理線) → 不會切斷遠端管理。
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(dirname "$SCRIPT_DIR")"
LOG="$PROJ/logs/net-watchdog.log"
PING_TARGET="8.8.8.8"

mkdir -p "$(dirname "$LOG")" 2>/dev/null
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [net-watchdog] $1" >> "$LOG"; }

# 動態解析 Wi-Fi 介面（不寫死 en1）——mini 有 en0~en7 多介面，硬編怕日後枚舉改變
# 而 toggle 到錯介面。listallhardwareports 純讀本機硬體、斷網時照樣可用；解析失敗退回 en1。
WIFI_DEV="$(/usr/sbin/networksetup -listallhardwareports 2>/dev/null \
    | awk '/Wi-Fi|AirPort/{getline; print $2; exit}')"
WIFI_DEV="${WIFI_DEV:-en1}"

# 連續 3 次 ping（每次逾時 3s、間隔 2s）任一成功即視為通；全失敗才算斷。
ping_ok() {
    local i
    for i in 1 2 3; do
        if /sbin/ping -c 1 -t 3 "$PING_TARGET" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

if ping_ok; then
    log "ok"
    exit 0
fi

log "外網不通（連 3 次 ping 失敗）→ 重連 Wi-Fi ${WIFI_DEV}"
/usr/sbin/networksetup -setairportpower "$WIFI_DEV" off 2>>"$LOG"
sleep 5
/usr/sbin/networksetup -setairportpower "$WIFI_DEV" on 2>>"$LOG"
sleep 25   # 等重新關聯 AP + DHCP 取得 IP

if ping_ok; then
    log "Wi-Fi 重連後已恢復連線"
    # 通知（key 從 .env runtime 讀、token 不經本腳本；localhost 此時必通）
    KEY="$(grep -E '^CSP_INTERNAL_KEY=' "$PROJ/.env" 2>/dev/null | head -1 | cut -d= -f2-)"
    /usr/bin/curl -s -m 10 -X POST -H "X-Internal-Key: $KEY" \
        "http://localhost:8000/api/internal/net-recovered" >/dev/null 2>&1 \
        && log "已發送復原通知" || log "復原通知發送失敗（不影響網路已恢復）"
else
    log "Wi-Fi 重連後仍不通（可能 router/WAN 端斷線，非 mini wifi）→ 30 分後再試"
fi
