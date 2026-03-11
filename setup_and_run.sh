#!/bin/bash
# ============================================
# CryptoSignal Pro — 一鍵安裝與回測
# ============================================

echo "🚀 CryptoSignal Pro 環境安裝與回測"
echo "======================================"

# Step 1: 安裝 Python 依賴
echo ""
echo "📦 Step 1: 安裝 Python 套件..."
pip3 install --user ccxt==4.4.10 pandas numpy fastapi uvicorn

if [ $? -ne 0 ]; then
    echo "❌ 安裝失敗！請檢查網路連線後重試。"
    exit 1
fi

echo "✅ 套件安裝完成！"

# Step 2: 執行回測
echo ""
echo "📊 Step 2: 開始執行 BTC 七年回測..."
echo "（這可能需要 5–10 分鐘，取決於網路速度和資料量）"
echo ""

cd "$(dirname "$0")/backend"
python3 run_backtest.py

echo ""
echo "======================================"
echo "✅ 完成！請查看以下檔案："
echo "   📄 回測報告: data/backtest_report.txt"
echo "======================================"
