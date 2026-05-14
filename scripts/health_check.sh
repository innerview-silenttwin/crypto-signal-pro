#!/bin/bash
#
# health_check.sh — 一鍵看系統健康狀態
#
# Mac mini 上跑：
#   bash scripts/health_check.sh
#
# 也可以加進 crontab 每天早上自動跑、結果寫進檔案
#   0 8 * * * bash ~/Documents/plate/crypto-signal-pro/scripts/health_check.sh > /tmp/health.txt

set -u

# 自動偵測專案目錄（讓 MacBook / Mac mini 共用同一份腳本）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$APP_DIR/logs"
DATA_DIR="$APP_DIR/data"

TODAY=$(date +%Y-%m-%d)
NOW=$(date "+%Y-%m-%d %H:%M:%S")

echo "═══════════════════════════════════════════════════════════"
echo "   crypto-signal-pro 健康檢查 ($NOW)"
echo "   $APP_DIR"
echo "═══════════════════════════════════════════════════════════"

# ── 1. launchd 服務狀態 ──
echo ""
echo "▶ 服務狀態"
echo "─────────────────────────────────────────"
SVC=$(launchctl list 2>/dev/null | grep crypto-signal-pro || true)
if [ -n "$SVC" ]; then
    PID=$(echo "$SVC" | awk '{print $1}')
    EXIT=$(echo "$SVC" | awk '{print $2}')
    LABEL=$(echo "$SVC" | awk '{print $3}')
    if [ "$PID" = "-" ]; then
        echo "❌ $LABEL 沒在跑 (上次 exit code: $EXIT)"
    else
        echo "✅ $LABEL 運行中 (PID $PID, last exit $EXIT)"
    fi
else
    echo "⚠️  找不到服務（可能是手動跑或 launchd 沒安裝）"
fi

# ── 2. 最新日誌 ──
echo ""
echo "▶ 最新日誌（log 最後 20 行）"
echo "─────────────────────────────────────────"
if [ -f "$LOG_DIR/crypto-signal-pro.log" ]; then
    SIZE=$(wc -c < "$LOG_DIR/crypto-signal-pro.log" | tr -d ' ')
    echo "📄 大小: $SIZE bytes"
    if [ "$SIZE" -gt 0 ]; then
        tail -20 "$LOG_DIR/crypto-signal-pro.log"
    else
        echo "(空檔 — 可能 stdout 被 buffer，或服務沒輸出)"
    fi
else
    echo "(沒有 log 檔)"
fi

# ── 3. 最新錯誤 ──
echo ""
echo "▶ 最新錯誤（error log 最後 20 行）"
echo "─────────────────────────────────────────"
if [ -f "$LOG_DIR/crypto-signal-pro-error.log" ]; then
    SIZE=$(wc -c < "$LOG_DIR/crypto-signal-pro-error.log" | tr -d ' ')
    echo "📄 大小: $SIZE bytes"
    if [ "$SIZE" -gt 0 ]; then
        tail -20 "$LOG_DIR/crypto-signal-pro-error.log"
    else
        echo "(空檔)"
    fi
else
    echo "(沒有 error log 檔)"
fi

# ── 4. Broker 狀態 ──
echo ""
echo "▶ Broker 內部狀態 (data/broker_state.json)"
echo "─────────────────────────────────────────"
python3 - <<PY
import json, os, sys, time
from datetime import date

path = "$DATA_DIR/broker_state.json"
if not os.path.exists(path):
    print("(沒有 broker_state.json — 系統還在 VirtualBroker 或還沒啟動 Sinopac)")
    sys.exit(0)

try:
    with open(path) as f:
        s = json.load(f)
except Exception as e:
    print(f"⚠️  讀取失敗：{e}")
    sys.exit(0)

today = date.today().isoformat()
print(f"📅 今日: $TODAY")

# pending orders
po = s.get("pending_orders", {})
print(f"⏳ 在飛訂單: {len(po)} 筆")
for sym, order in po.items():
    print(f"   - {sym}: {order.get('action')} {order.get('qty_shares')}股 @ {order.get('limit_price')}")
    submitted_at = order.get("submitted_at", 0)
    if submitted_at:
        age = time.time() - submitted_at
        if age > 300:
            print(f"     ⚠️  已送出 {age/60:.1f} 分鐘還沒成交")

# daily orders / sector
do = s.get("daily_orders", {}).get(today, {})
if do:
    total = sum(do.values()) if isinstance(do, dict) else 0
    print(f"📊 今日下單數 (total {total}): {dict(do)}")
else:
    print(f"📊 今日下單數: 0")

# realized PnL
pnl = s.get("daily_realized_pnl", {}).get(today, {})
if pnl:
    total_pnl = sum(pnl.values()) if isinstance(pnl, dict) else 0
    sign = "📈" if total_pnl >= 0 else "📉"
    print(f"{sign} 今日已實現損益 (total {total_pnl:+.0f}): {dict(pnl)}")
else:
    print(f"📊 今日已實現損益: 0")

# daily lock (kill switch)
lock = s.get("daily_lock", {}).get(today)
if lock:
    print(f"🔒 ⚠️  KILL SWITCH 已啟動: {lock}")
else:
    print(f"🔓 daily_lock: 未觸發")

# cooldowns: flat dict {f"{sector}:{symbol}:{action}": expires_at}
cd = s.get("cooldowns", {})
now = time.time()
active = []
for key, expires in cd.items():
    try:
        if float(expires) > now:
            mins = (float(expires) - now) / 60
            active.append((key, mins))
    except (TypeError, ValueError):
        continue
active.sort(key=lambda x: -x[1])
print(f"⏱  進行中冷卻 ({len(active)} 筆):")
for key, mins in active[:10]:
    print(f"   - {key} 剩 {mins:.0f} 分鐘")
if len(active) > 10:
    print(f"   ... 還有 {len(active)-10} 筆")
PY

# ── 5. 今日被擋的單 ──
echo ""
echo "▶ 今日 RiskGate 擋下的單 (skipped_trades.jsonl)"
echo "─────────────────────────────────────────"
SKIPPED="$DATA_DIR/skipped_trades.jsonl"
if [ -f "$SKIPPED" ]; then
    TODAY_COUNT=$(grep -c "$TODAY" "$SKIPPED" 2>/dev/null || true)
    echo "📊 今日被擋: $TODAY_COUNT 筆"
    if [ "$TODAY_COUNT" -gt 0 ]; then
        grep "$TODAY" "$SKIPPED" | tail -10 | python3 -c "
import sys, json
for line in sys.stdin:
    try:
        d = json.loads(line)
        ts = d.get('ts','')[11:19]
        sym = d.get('symbol','?')
        act = d.get('action','?')
        reason = d.get('reason','?')
        print(f'  {ts} {sym} {act} → {reason}')
    except Exception:
        print(f'  (parse error)')
"
    fi
else
    echo "(沒有 skipped_trades.jsonl)"
fi

# ── 6. Sinopac place_order 健康度（從 log 解析）──
echo ""
echo "▶ Sinopac place_order 健康度 (今日)"
echo "─────────────────────────────────────────"
LOG="$LOG_DIR/crypto-signal-pro.log"
if [ -f "$LOG" ]; then
    # log 沒按日期切，用 grep 估自服務啟動以來
    # 用 ${VAR:-0} 預設值避免 set -u 撞 unbound、用 ${} braces 隔開全形字元
    TIMEOUT_COUNT=$(grep -c "place_order timeout (attempt" "$LOG" 2>/dev/null || true)
    AFTER_RETRY_FAIL=$(grep -c "place_order timeout after.*attempts" "$LOG" 2>/dev/null || true)
    NOT_READY=$(grep -c "sol.cpp.*Not ready" "$LOG" 2>/dev/null || true)
    SESSION_UP=$(grep -c "Event: Session up" "$LOG" 2>/dev/null || true)
    echo "  Solace 'Session up' 次數: ${SESSION_UP:-0}（健康 1-2 次；> 5 表示連線抖動）"
    echo "  Solace 'Not ready' 錯誤: ${NOT_READY:-0}"
    echo "  place_order timeout（觸發 retry）: ${TIMEOUT_COUNT:-0}"
    echo "  retry 後仍失敗: ${AFTER_RETRY_FAIL:-0}"
    if [ "${AFTER_RETRY_FAIL:-0}" -gt 0 ]; then
        echo "  ⚠️  仍有 ${AFTER_RETRY_FAIL} 筆完全失敗，請檢查永豐 simulation 主機狀態"
    fi
else
    echo "  (沒有 log 檔可解析)"
fi

# ── 7. 設定檢查 ──
echo ""
echo "▶ Broker 模式設定 (.env)"
echo "─────────────────────────────────────────"
ENV_FILE="$APP_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    BROKER_MODE=$(grep "^BROKER_MODE=" "$ENV_FILE" | head -1 | cut -d= -f2 || echo "")
    SIM=$(grep "^SHIOAJI_SIMULATION=" "$ENV_FILE" | head -1 | cut -d= -f2 || echo "")
    SECTORS=$(grep "^ALLOWED_SECTORS=" "$ENV_FILE" | head -1 | cut -d= -f2 || echo "")
    LIVE=$(grep "^BROKER_LIVE_TRADING=" "$ENV_FILE" | head -1 | cut -d= -f2 || echo "")
    echo "  BROKER_MODE         = ${BROKER_MODE:-(未設, 預設 virtual)}"
    echo "  SHIOAJI_SIMULATION  = ${SIM:-(未設, 預設 true 安全)}"
    echo "  ALLOWED_SECTORS     = ${SECTORS:-(未設)}"
    echo "  BROKER_LIVE_TRADING = ${LIVE:-(未設, 預設 false)}"

    if [ "$BROKER_MODE" = "sinopac" ] && [ "$SIM" != "true" ]; then
        echo "  ⚠️  Sinopac 但 simulation 不是 true — 檢查是否誤觸真錢"
    fi
else
    echo "❌ 找不到 $ENV_FILE"
fi

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "   完成"
echo "═══════════════════════════════════════════════════════════"
