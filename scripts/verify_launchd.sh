#!/usr/bin/env bash
#
# 部署後 smoke test：驗 launchd 服務 + internal trigger endpoints 註冊 + auth 生效。
#
# 設計重點：**絕不真的觸發 trigger endpoint**（premarket/evening/daily-inst 一被 POST
# 就會發 Telegram）。改用零副作用方式驗證：
#   1. GET /api/ping        → 服務活著
#   2. GET /openapi.json    → 三個 trigger 路徑已註冊（不執行 handler）
#   3. 不帶 key POST → 預期 403（**僅當 .env 已設 CSP_INTERNAL_KEY 時**；未設代表
#      fail-open，POST 會誤觸發發訊，故跳過並警告）
#   4. launchd：三個 trigger job 是否 load（advisory，非部署機不算失敗）
#
# 用法：
#   bash scripts/verify_launchd.sh [BASE_URL]
#   CSP_ENV_FILE=/path/to/.env bash scripts/verify_launchd.sh http://10.0.0.1:8000
#
# 結束碼：全部關鍵檢查通過 0，否則 1（可供部署腳本判斷）。
set -uo pipefail

BASE_URL="${1:-http://localhost:8000}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${CSP_ENV_FILE:-$SCRIPT_DIR/../.env}"

TRIGGERS=(trigger-premarket-check trigger-evening-summary trigger-daily-inst-refresh)
LABELS=(local.crypto-premarket-trigger local.crypto-evening-trigger local.crypto-daily-inst-trigger local.crypto-net-watchdog local.crypto-ai-news)

fail=0
pass() { printf "  ✅ %s\n" "$1"; }
warn() { printf "  ⚠️  %s\n" "$1"; }
bad()  { printf "  ❌ %s\n" "$1"; fail=1; }

echo "▶ 1. 服務健康（GET /api/ping）"
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/api/ping" || echo 000)
if [ "$code" = "200" ]; then pass "/api/ping → 200"; else bad "/api/ping → $code（服務沒起來？）"; fi

echo "▶ 2. trigger endpoints 已註冊（GET /openapi.json，零副作用）"
spec=$(curl -s "$BASE_URL/openapi.json" || echo "")
for t in "${TRIGGERS[@]}"; do
  if printf '%s' "$spec" | grep -q "/api/internal/$t"; then
    pass "/api/internal/$t 已註冊"
  else
    bad "/api/internal/$t 不在 openapi（路由沒掛上？）"
  fi
done

echo "▶ 3. auth 生效（不帶 key POST → 403）"
KEY=""
[ -f "$ENV_FILE" ] && KEY="$(grep -E '^CSP_INTERNAL_KEY=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)"
if [ -z "$KEY" ]; then
  warn "CSP_INTERNAL_KEY 未設（fail-open）→ 跳過 POST 驗證，避免誤觸發 Telegram"
  warn "（若此處應為已啟用 auth 的部署機，代表 .env 漏設金鑰，請補上）"
else
  for t in "${TRIGGERS[@]}"; do
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/internal/$t" || echo 000)
    if [ "$code" = "403" ]; then pass "/api/internal/$t 不帶 key → 403"; else bad "/api/internal/$t 不帶 key → $code（預期 403）"; fi
  done
fi

echo "▶ 4. launchd trigger jobs 已 load（advisory）"
if command -v launchctl >/dev/null 2>&1; then
  loaded=$(launchctl list 2>/dev/null || echo "")
  for l in "${LABELS[@]}"; do
    if printf '%s' "$loaded" | grep -q "$l"; then pass "$l 已 load"; else warn "$l 未 load（若在部署機請 launchctl load）"; fi
  done
else
  warn "無 launchctl（非 macOS 部署機）→ 跳過"
fi

echo
if [ "$fail" -eq 0 ]; then echo "✅ 關鍵檢查全部通過"; else echo "❌ 有關鍵檢查失敗，請查上方 ❌"; fi
exit "$fail"
