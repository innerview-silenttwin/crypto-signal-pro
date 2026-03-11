"""
BTC 七年回測主程式

功能：
1. 透過 CCXT 下載 BTC/USDT 歷史 K 線數據（最多 7 年）
2. 針對短期、中期、長期分別回測
3. 測試不同信號門檻的效果
4. 產出完整回測報告
"""

import sys
import os
import time
from datetime import datetime, timedelta
from typing import List, Dict

import pandas as pd
import ccxt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine, BacktestResult


def fetch_historical_data(
    symbol: str = "BTC/USDT",
    timeframe: str = "1d",
    since_years: int = 7,
    exchange_id: str = "binance",
) -> pd.DataFrame:
    """
    從交易所下載歷史 K 線數據
    
    Args:
        symbol: 交易對
        timeframe: K 線週期 (1d, 4h, 1h 等)
        since_years: 下載多少年的數據
        exchange_id: 交易所
    Returns:
        DataFrame with columns: open, high, low, close, volume
    """
    print(f"\n📡 正在從 {exchange_id} 下載 {symbol} {timeframe} 數據...")
    print(f"   回溯期間: {since_years} 年")
    
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'},
    })
    
    # 計算起始時間
    since = int((datetime.now() - timedelta(days=since_years * 365)).timestamp() * 1000)
    
    all_ohlcv = []
    fetch_since = since
    batch_count = 0
    
    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=fetch_since, limit=1000)
            if not ohlcv:
                break
            
            all_ohlcv.extend(ohlcv)
            batch_count += 1
            
            # 更新 since 到最後一根 K 線之後
            fetch_since = ohlcv[-1][0] + 1
            
            if batch_count % 5 == 0:
                print(f"   已下載 {len(all_ohlcv)} 根 K 線...")
            
            # 如果取回的數量少於 limit，表示已經到最新
            if len(ohlcv) < 1000:
                break
            
            time.sleep(exchange.rateLimit / 1000)
            
        except Exception as e:
            print(f"   ⚠️ 下載錯誤: {e}")
            time.sleep(2)
            continue
    
    # 轉換為 DataFrame
    df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)
    df = df[~df.index.duplicated(keep='first')]
    df.sort_index(inplace=True)
    
    print(f"   ✅ 下載完成！共 {len(df)} 根 K 線")
    print(f"   期間: {df.index[0]} ~ {df.index[-1]}")
    
    return df


def save_data(df: pd.DataFrame, filename: str):
    """儲存數據到 CSV"""
    filepath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    df.to_csv(filepath)
    print(f"   💾 數據已儲存: {filepath}")
    return filepath


def load_data(filename: str) -> pd.DataFrame:
    """從 CSV 載入數據"""
    filepath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', filename)
    if os.path.exists(filepath):
        df = pd.read_csv(filepath, index_col='timestamp', parse_dates=True)
        print(f"   📂 從快取載入: {filepath} ({len(df)} 根 K 線)")
        return df
    return None


def run_comprehensive_backtest():
    """執行完整回測"""
    
    print("=" * 70)
    print("🚀 CryptoSignal Pro — BTC 七年回測")
    print("=" * 70)
    
    # ── 1. 準備數據 ──
    # 日線數據（中長期回測）
    daily_data = load_data('btc_daily_7y.csv')
    if daily_data is None:
        daily_data = fetch_historical_data('BTC/USDT', '1d', since_years=7)
        save_data(daily_data, 'btc_daily_7y.csv')
    
    # 4 小時線數據（中期回測）—— 只能取約2年
    h4_data = load_data('btc_4h_2y.csv')
    if h4_data is None:
        h4_data = fetch_historical_data('BTC/USDT', '4h', since_years=2)
        save_data(h4_data, 'btc_4h_2y.csv')
    
    # 1 小時線數據（短期回測）—— 只能取約1年
    h1_data = load_data('btc_1h_1y.csv')
    if h1_data is None:
        h1_data = fetch_historical_data('BTC/USDT', '1h', since_years=1)
        save_data(h1_data, 'btc_1h_1y.csv')
    
    all_results: List[BacktestResult] = []
    
    # ── 2. 回測場景 (優化版：尋找高頻率+穩健獲利的商業最佳解) ──
    scenarios = [
        # (名稱, 數據, 時間框架, 買入門檻, 賣出門檻, 停損%, 停利%, 類別)
        ("短期-突破", h1_data, "1h", 40, 40, 4.0, 8.0, "短期"),
        ("中期-波段(4H)", h4_data, "4h", 45, 45, 6.0, 15.0, "中期"),
        ("中期-波段(4H放寬)", h4_data, "4h", 35, 35, 8.0, 20.0, "中期"),
        
        # 重點測試：日線級別。降低門檻增加交易次數，縮小停損停利優化勝率
        ("日線-順勢波段", daily_data, "1d", 45, 45, 12.0, 25.0, "長中"),
        ("日線-高頻(門檻40)", daily_data, "1d", 40, 40, 15.0, 30.0, "長中"),
        ("日線-超高頻(門檻30)", daily_data, "1d", 30, 30, 15.0, 35.0, "長中"),
        
        # 模擬三重濾網概念 (嚴格大趨勢停損)
        ("日線-嚴格停損", daily_data, "1d", 40, 40, 8.0, 35.0, "長中"),
    ]
    
    for name, data, tf, buy_th, sell_th, sl, tp, category in scenarios:
        print(f"\n{'─' * 70}")
        print(f"📋 場景: {name} ({category})")
        print(f"{'─' * 70}")
        
        engine = BacktestEngine()
        result = engine.run(
            df=data,
            symbol="BTC/USDT",
            timeframe=tf,
            buy_threshold=buy_th,
            sell_threshold=sell_th,
            stop_loss_pct=sl,
            take_profit_pct=tp,
        )
        all_results.append((name, category, result))
        print(result.report())
    
    # ── 3. 綜合比較報告 ──
    print("\n" + "=" * 70)
    print("📊 綜合比較報告")
    print("=" * 70)
    
    print(f"\n{'場景':<20} {'類別':<6} {'交易次數':<8} {'勝率':<8} "
          f"{'累計獲利':<12} {'最大回撤':<10} {'夏普比率':<10} {'獲利因子':<10}")
    print("-" * 84)
    
    for name, category, result in all_results:
        print(f"{name:<20} {category:<6} {result.total_trades:<8} "
              f"{result.win_rate:<8.1f}% {result.total_profit_pct:<12.2f}% "
              f"{result.max_drawdown_pct:<10.2f}% {result.sharpe_ratio:<10.2f} "
              f"{result.profit_factor:<10.2f}")
    
    # ── 4. 最佳策略推薦 ──
    print("\n" + "=" * 70)
    print("🏆 最佳策略推薦")
    print("=" * 70)
    
    # 按勝率排序
    sorted_by_winrate = sorted(all_results, key=lambda x: x[2].win_rate, reverse=True)
    print(f"\n📈 勝率最高: {sorted_by_winrate[0][0]} ({sorted_by_winrate[0][2].win_rate:.1f}%)")
    
    # 按累計獲利排序
    sorted_by_profit = sorted(all_results, key=lambda x: x[2].total_profit_pct, reverse=True)
    print(f"💰 獲利最高: {sorted_by_profit[0][0]} ({sorted_by_profit[0][2].total_profit_pct:.2f}%)")
    
    # 按夏普比率排序
    sorted_by_sharpe = sorted(all_results, key=lambda x: x[2].sharpe_ratio, reverse=True)
    print(f"⚖️  風險調節最佳: {sorted_by_sharpe[0][0]} (夏普比率: {sorted_by_sharpe[0][2].sharpe_ratio:.2f})")
    
    # ── 5. 儲存報告 ──
    report_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'backtest_report.txt'
    )
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(f"CryptoSignal Pro — BTC 回測報告\n")
        f.write(f"產生時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        for name, category, result in all_results:
            f.write(f"\n{'─' * 70}\n")
            f.write(f"場景: {name} ({category})\n")
            f.write(result.report())
            f.write("\n")
    
    print(f"\n💾 完整報告已儲存: {report_path}")
    print("\n✅ 所有回測完成！")
    
    return all_results


if __name__ == "__main__":
    results = run_comprehensive_backtest()
