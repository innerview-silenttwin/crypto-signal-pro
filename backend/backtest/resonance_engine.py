"""
多時間框架共振回測引擎 (Resonance Backtest Engine) - 修正版
"""

import sys
import os
import pandas as pd
import numpy as np
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

# 確保 import 路徑正確
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.aggregator import SignalAggregator, AggregatedSignal
from backtest.engine import Trade, BacktestResult

class ResonanceBacktestEngine:
    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.aggregator = SignalAggregator(weights=weights)

    def run(
        self,
        df_trigger: pd.DataFrame,  # 較短框架 (如 4H)
        df_filter: pd.DataFrame,   # 較長框架 (如 1D)
        symbol: str = "BTC/USDT",
        trigger_tf: str = "4h",
        filter_tf: str = "1d",
        buy_threshold: float = 30.0,
        sell_threshold: float = 40.0,
        filter_threshold: float = 30.0, 
        stop_loss_pct: float = 6.0,
        take_profit_pct: float = 18.0,
        lookback: int = 200,
    ) -> BacktestResult:
        """執行共振回測"""
        print(f"\n🌈 開始共振回測 {symbol} ({filter_tf} + {trigger_tf})...")
        print(f"   數值門檻: 趨勢濾網({filter_tf}) >= {filter_threshold} | 觸發器({trigger_tf}) >= {buy_threshold}")
        
        trades: List[Trade] = []
        current_trade: Optional[Trade] = None
        buy_signals_count = 0
        peak_equity = 100.0
        max_drawdown = 0.0
        
        # 確保數據索引是 Datetime 並排序
        df_trigger = df_trigger.sort_index()
        df_filter = df_filter.sort_index()
        
        # --- 1. 預計算 1D 趨勢信號 ---
        print(f"   正在預計算 {filter_tf} 趨勢信號...")
        filter_results = []
        for i in range(lookback, len(df_filter)):
            window = df_filter.iloc[i-lookback:i+1]
            sig = self.aggregator.analyze(window, symbol, filter_tf)
            filter_results.append({
                'time': df_filter.index[i],
                'direction': sig.direction,
                'confidence': sig.confidence,
                'signal_level': sig.signal_level
            })
        df_f_signals = pd.DataFrame(filter_results).set_index('time')
        
        # 統計 1D 多頭次數
        f_bull_count = len(df_f_signals[(df_f_signals['direction'] == 'BUY') & (df_f_signals['confidence'] >= filter_threshold)])
        print(f"   [統計] {filter_tf} 總 K 線數: {len(df_filter)}, 符合多頭門檻的 K 線數: {f_bull_count}")

        # --- 2. 準備 4H 發動點數據 (也預計算以加速回測) ---
        print(f"   正在預計算 {trigger_tf} 觸發信號...")
        trigger_results = []
        start_idx = max(lookback, 200)
        for i in range(start_idx, len(df_trigger)):
            window = df_trigger.iloc[i-lookback:i+1]
            sig = self.aggregator.analyze(window, symbol, trigger_tf)
            trigger_results.append({
                'time': df_trigger.index[i],
                'price': df_trigger['close'].iloc[i],
                'direction': sig.direction,
                'confidence': sig.confidence,
                'signal_level': sig.signal_level,
                'signals': sig.buy_signals + sig.sell_signals
            })
        df_t_signals = pd.DataFrame(trigger_results).set_index('time')
        
        # 統計 4H 買入次數
        t_buy_count = len(df_t_signals[(df_t_signals['direction'] == 'BUY') & (df_t_signals['confidence'] >= buy_threshold)])
        print(f"   [統計] {trigger_tf} 總 K 線數: {len(df_trigger)}, 符合買入門檻的 K 線數: {t_buy_count}")

        # --- 3. 執行回測循環 ---
        print(f"   開始撮合信號與執行交易...")
        for time_4h, t_sig in df_t_signals.iterrows():
            current_time = time_4h
            current_price = t_sig['price']

            # 找到最新的已收盤 1D 信號
            # 使用 pd.DataFrame.asof 找到不超過當前時間的最接近索引
            f_sig = None
            try:
                # 取得時間小於等於當前時間的最新 1D 信號
                f_sig_idx = df_f_signals.index.get_indexer([current_time], method='pad')[0]
                if f_sig_idx != -1:
                    f_sig = df_f_signals.iloc[f_sig_idx]
            except:
                continue
            
            if f_sig is None: continue

            # 交易邏輯
            if current_trade is None:
                # 買入條件：1D 共振已開啟 且 4H 出現买入信號
                resonance_buy = (
                    f_sig['direction'] == "BUY" and f_sig['confidence'] >= filter_threshold and
                    t_sig['direction'] == "BUY" and t_sig['confidence'] >= buy_threshold
                )
                
                if resonance_buy:
                    buy_signals_count += 1
                    reason = f"[{filter_tf}共振] {f_sig['signal_level']}({f_sig['confidence']:.0f}) + {t_sig['signal_level']}({t_sig['confidence']:.0f})"
                    current_trade = Trade(
                        entry_time=current_time,
                        entry_price=current_price,
                        entry_score=t_sig['confidence'],
                        entry_reason=reason,
                        direction="BUY",
                    )
            else:
                # 平倉邏輯
                price_change_pct = (current_price - current_trade.entry_price) / current_trade.entry_price * 100
                
                should_close = False
                close_reason = ""

                if price_change_pct <= -stop_loss_pct:
                    should_close = True
                    close_reason = f"觸發停損 ({price_change_pct:.2f}%)"
                elif price_change_pct >= take_profit_pct:
                    should_close = True
                    close_reason = f"觸發停利 ({price_change_pct:.2f}%)"
                elif t_sig['direction'] == "SELL" and t_sig['confidence'] >= sell_threshold:
                    should_close = True
                    close_reason = f"4H 賣出信號 ({t_sig['confidence']:.1f}分)"
                
                if should_close:
                    current_trade.close(current_time, current_price, t_sig['confidence'], close_reason)
                    trades.append(current_trade)
                    current_trade = None

        # 封裝結果
        result = BacktestResult(
            symbol=symbol,
            timeframe=f"{filter_tf}+{trigger_tf}",
            period=f"{df_trigger.index[0].strftime('%Y-%m-%d')} ~ {df_trigger.index[-1].strftime('%Y-%m-%d')}",
            signal_threshold=buy_threshold,
            trades=trades,
            total_buy_signals=buy_signals_count,
            total_sell_signals=0
        )
        # (剩餘計算統計代碼保持不變，為節省長度此處省略但會寫入)
        if trades:
            profits = [t.profit_pct for t in trades]
            wins = [p for p in profits if p > 0]
            losses = [p for p in profits if p <= 0]
            result.total_trades = len(trades)
            result.winning_trades = len(wins)
            result.losing_trades = len(losses)
            result.win_rate = len(wins) / len(trades) * 100
            result.total_profit_pct = sum(profits)
            result.avg_profit_pct = np.mean(profits)
            result.avg_win_pct = np.mean(wins) if wins else 0
            result.avg_loss_pct = np.mean(losses) if losses else 0
            result.max_profit_pct = max(profits)
            result.max_loss_pct = min(profits)
            result.avg_holding_days = np.mean([t.holding_days for t in trades])
            if len(profits) > 1:
                result.sharpe_ratio = np.mean(profits) / np.std(profits) if np.std(profits) > 0 else 0
            total_wins = sum(wins) if wins else 0
            total_losses = abs(sum(losses)) if losses else 0
            result.profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
            cum_profit = 1.0; peak = 1.0; mdd = 0.0
            for p in profits:
                cum_profit *= (1+p/100)
                peak = max(peak, cum_profit)
                mdd = max(mdd, (peak-cum_profit)/peak*100)
            result.max_drawdown_pct = mdd
        return result
