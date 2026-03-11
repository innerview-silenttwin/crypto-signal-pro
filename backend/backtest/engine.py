"""
回測引擎 (Backtesting Engine)

核心功能：
1. 載入歷史 OHLCV 數據
2. 滑動窗口逐 K 線計算信號
3. 模擬交易並記錄績效
4. 產出詳細回測報告
"""

import sys
import os
import json
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.aggregator import SignalAggregator, AggregatedSignal


@dataclass
class Trade:
    """單筆交易紀錄"""
    entry_time: pd.Timestamp
    entry_price: float
    entry_score: float
    entry_reason: str
    direction: str          # BUY / SELL (做多 / 做空)
    
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_score: Optional[float] = None
    exit_reason: str = ""
    
    profit_pct: float = 0.0
    profit_usd: float = 0.0
    holding_days: float = 0.0

    @property
    def is_closed(self) -> bool:
        return self.exit_time is not None

    def close(self, exit_time, exit_price, exit_score=0, exit_reason=""):
        self.exit_time = exit_time
        self.exit_price = exit_price
        self.exit_score = exit_score
        self.exit_reason = exit_reason
        
        if self.direction == "BUY":
            self.profit_pct = (exit_price - self.entry_price) / self.entry_price * 100
        else:  # SHORT
            self.profit_pct = (self.entry_price - exit_price) / self.entry_price * 100
        
        self.profit_usd = self.profit_pct / 100 * self.entry_price
        self.holding_days = (exit_time - self.entry_time).total_seconds() / 86400


@dataclass
class BacktestResult:
    """回測結果報告"""
    symbol: str
    timeframe: str
    period: str
    signal_threshold: float
    
    # 整體績效 
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    
    # 獲利
    total_profit_pct: float = 0.0
    avg_profit_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    max_profit_pct: float = 0.0
    max_loss_pct: float = 0.0
    
    # 風險指標
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    
    # 持倉
    avg_holding_days: float = 0.0
    
    # 交易明細
    trades: List[Trade] = field(default_factory=list)
    
    # 信號統計
    total_buy_signals: int = 0
    total_sell_signals: int = 0
    
    def report(self) -> str:
        """產出文字報告"""
        lines = [
            "=" * 70,
            f"📊 回測報告 | {self.symbol} | {self.timeframe} | {self.period}",
            f"   信號門檻: ≥ {self.signal_threshold} 分",
            "=" * 70,
            "",
            f"【交易統計】",
            f"  總交易次數:   {self.total_trades}",
            f"  勝出交易:     {self.winning_trades}",
            f"  虧損交易:     {self.losing_trades}",
            f"  ✅ 勝率:       {self.win_rate:.1f}%",
            "",
            f"【獲利表現】",
            f"  累計獲利:     {self.total_profit_pct:+.2f}%",
            f"  平均每筆獲利: {self.avg_profit_pct:+.2f}%",
            f"  平均勝出獲利: {self.avg_win_pct:+.2f}%",
            f"  平均虧損:     {self.avg_loss_pct:+.2f}%",
            f"  最大單筆獲利: {self.max_profit_pct:+.2f}%",
            f"  最大單筆虧損: {self.max_loss_pct:+.2f}%",
            "",
            f"【風險指標】",
            f"  最大回撤:     {self.max_drawdown_pct:.2f}%",
            f"  夏普比率:     {self.sharpe_ratio:.2f}",
            f"  獲利因子:     {self.profit_factor:.2f}",
            "",
            f"【持倉統計】",
            f"  平均持倉天數: {self.avg_holding_days:.1f} 天",
            "",
            f"【信號統計】",
            f"  買入信號總數: {self.total_buy_signals}",
            f"  賣出信號總數: {self.total_sell_signals}",
            "=" * 70,
        ]
        return "\n".join(lines)


class BacktestEngine:
    """回測引擎"""

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.aggregator = SignalAggregator(weights=weights)

    def run(
        self,
        df: pd.DataFrame,
        symbol: str = "BTC/USDT",
        timeframe: str = "1d",
        buy_threshold: float = 50.0,
        sell_threshold: float = 50.0,
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 15.0,
        lookback: int = 200,
    ) -> BacktestResult:
        """
        執行回測
        
        Args:
            df: 歷史 OHLCV DataFrame
            symbol: 交易對
            timeframe: 時間框架
            buy_threshold: 買入信號門檻分數
            sell_threshold: 賣出信號門檻分數
            stop_loss_pct: 停損百分比
            take_profit_pct: 停利百分比
            lookback: 每次計算指標時使用的回溯行數
        """
        print(f"\n🔄 開始回測 {symbol} ({timeframe})...")
        print(f"   數據範圍: {df.index[0]} ~ {df.index[-1]}")
        print(f"   總 K 線數: {len(df)}")
        print(f"   買入門檻: {buy_threshold}分 | 賣出門檻: {sell_threshold}分")
        print(f"   停損: {stop_loss_pct}% | 停利: {take_profit_pct}%")
        
        trades: List[Trade] = []
        current_trade: Optional[Trade] = None
        buy_signals_count = 0
        sell_signals_count = 0
        equity_curve = []
        peak_equity = 100.0
        max_drawdown = 0.0
        
        # 確保有足夠的回溯數據
        start_idx = max(lookback, 200)
        total_bars = len(df) - start_idx
        report_interval = max(1, total_bars // 20)  # 每5%報告一次進度
        
        for i in range(start_idx, len(df)):
            # 進度回報
            progress = i - start_idx
            if progress % report_interval == 0:
                pct = progress / total_bars * 100
                print(f"   進度: {pct:.0f}% ({progress}/{total_bars})")
            
            # 取出回溯窗口的數據
            window = df.iloc[max(0, i - lookback):i + 1].copy()
            current_time = df.index[i]
            current_price = df['close'].iloc[i]
            
            # 計算所有指標
            try:
                window = self.aggregator.calculate_all(window)
                signal = self.aggregator.generate_signals(window, symbol, timeframe)
            except Exception as e:
                continue
            
            # 記錄信號數
            if signal.direction == "BUY" and signal.confidence >= buy_threshold:
                buy_signals_count += 1
            elif signal.direction == "SELL" and signal.confidence >= sell_threshold:
                sell_signals_count += 1
            
            # 交易邏輯
            if current_trade is None:
                # 無持倉 → 檢查是否開倉
                if signal.direction == "BUY" and signal.confidence >= buy_threshold:
                    reasons = "; ".join([f"{s.indicator_name}: {s.reason}" for s in signal.buy_signals])
                    current_trade = Trade(
                        entry_time=current_time,
                        entry_price=current_price,
                        entry_score=signal.confidence,
                        entry_reason=reasons,
                        direction="BUY",
                    )
            else:
                # 有持倉 → 檢查是否平倉
                price_change_pct = (current_price - current_trade.entry_price) / current_trade.entry_price * 100
                
                should_close = False
                close_reason = ""
                
                # 停損
                if price_change_pct <= -stop_loss_pct:
                    should_close = True
                    close_reason = f"觸發停損 ({price_change_pct:.2f}%)"
                # 停利
                elif price_change_pct >= take_profit_pct:
                    should_close = True
                    close_reason = f"觸發停利 ({price_change_pct:.2f}%)"
                # 賣出信號
                elif signal.direction == "SELL" and signal.confidence >= sell_threshold:
                    should_close = True
                    reasons = "; ".join([f"{s.indicator_name}: {s.reason}" for s in signal.sell_signals])
                    close_reason = f"賣出信號 ({signal.confidence:.1f}分): {reasons}"
                
                if should_close:
                    current_trade.close(
                        exit_time=current_time,
                        exit_price=current_price,
                        exit_score=signal.confidence,
                        exit_reason=close_reason,
                    )
                    trades.append(current_trade)
                    current_trade = None
            
            # 更新權益曲線（用於計算最大回撤）
            if trades:
                cumulative = 100.0
                for t in trades:
                    cumulative *= (1 + t.profit_pct / 100)
                equity_curve.append(cumulative)
                peak_equity = max(peak_equity, cumulative)
                drawdown = (peak_equity - cumulative) / peak_equity * 100
                max_drawdown = max(max_drawdown, drawdown)
        
        # 如果還有未平倉的交易，以最後價格平倉
        if current_trade is not None:
            current_trade.close(
                exit_time=df.index[-1],
                exit_price=df['close'].iloc[-1],
                exit_reason="回測結束平倉",
            )
            trades.append(current_trade)
        
        # 計算統計數據
        result = BacktestResult(
            symbol=symbol,
            timeframe=timeframe,
            period=f"{df.index[0].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}",
            signal_threshold=buy_threshold,
            trades=trades,
            total_buy_signals=buy_signals_count,
            total_sell_signals=sell_signals_count,
        )
        
        if trades:
            profits = [t.profit_pct for t in trades]
            wins = [p for p in profits if p > 0]
            losses = [p for p in profits if p <= 0]
            
            result.total_trades = len(trades)
            result.winning_trades = len(wins)
            result.losing_trades = len(losses)
            result.win_rate = len(wins) / len(trades) * 100 if trades else 0
            
            result.total_profit_pct = sum(profits)
            result.avg_profit_pct = np.mean(profits) if profits else 0
            result.avg_win_pct = np.mean(wins) if wins else 0
            result.avg_loss_pct = np.mean(losses) if losses else 0
            result.max_profit_pct = max(profits) if profits else 0
            result.max_loss_pct = min(profits) if profits else 0
            
            result.max_drawdown_pct = max_drawdown
            result.avg_holding_days = np.mean([t.holding_days for t in trades])
            
            # 夏普比率 (假設無風險利率 0)
            if len(profits) > 1:
                result.sharpe_ratio = np.mean(profits) / np.std(profits) if np.std(profits) > 0 else 0
            
            # 獲利因子
            total_wins = sum(wins) if wins else 0
            total_losses = abs(sum(losses)) if losses else 0
            result.profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
        
        print(f"\n✅ 回測完成！")
        return result
