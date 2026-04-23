"""
回測引擎 - 技術指標組合回測 (Combo Backtesting Engine)

根據 docs/backtest_system_spec.md 設計規格實作。
核心功能：
1. 逐日產生所有指標信號 (prepare_signals)
2. 對單一指標組合執行回測 (run_single_combo)
3. 排列組合所有指標，批量回測 (run_all_combos_with_progress)
4. 計算績效指標 (_calc_metrics)

效能設計：
- prepare_signals 做一次逐日信號產生（較慢），結果存為 numpy 陣列
- run_single_combo 全部用 numpy 陣列操作，不用 df.iloc（極快）
- 只保留前 100 名的交易明細，減少 JSON 序列化負擔
"""

import numpy as np
import pandas as pd
from itertools import combinations
from typing import List, Dict, Tuple, Callable, Optional

from indicators.base import BaseIndicator


# 顯示名稱對照表（用於前端呈現）
INDICATOR_DISPLAY = {
    "rsi":              "RSI 相對強弱",
    "macd":             "MACD 趨勢動能",
    "bollinger":        "布林通道",
    "mfi":              "MFI 資金流量",
    "ema_cross":        "EMA 均線交叉",
    "volume":           "成交量分析",
    "adx":              "ADX 趨勢強度",
    "stoch_rsi":        "隨機RSI",
    "volume_reversal":  "爆量反轉",
    "pullback_support": "均線拉回支撐",
    "bias":             "乖離率 (BIAS)",
    "kd":               "KD 隨機指標",
    "williams_r":       "威廉指標 %R",
}


class BacktestEngine:
    """技術指標組合回測引擎"""

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        commission_rate: float = 0.001425,
        tax_rate: float = 0.003,
        slippage: float = 0.0,
    ):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.tax_rate = tax_rate
        self.slippage = slippage
        self.warmup_period = 200  # EMA200 需要的暖機期

    def prepare_signals(
        self,
        df: pd.DataFrame,
        indicators: List[BaseIndicator],
        indicator_keys_map: Dict[str, str],
    ) -> pd.DataFrame:
        """
        Phase 1：計算所有指標欄位，並逐日產生每個指標的信號。

        Args:
            df: OHLCV DataFrame
            indicators: 所有指標實例
            indicator_keys_map: {indicator.name: registry_key}

        Returns:
            附加了 signal_{key} 欄位的 DataFrame
        """
        # 1. 一次性計算所有指標欄位（向量化，很快）
        for ind in indicators:
            try:
                df = ind.calculate(df)
            except Exception:
                pass

        # 2. 逐日產生信號（較慢，但只需做一次）
        warmup = self.warmup_period
        for ind in indicators:
            key = indicator_keys_map.get(ind.name)
            if key is None:
                continue

            signals = []
            for i in range(len(df)):
                if i < warmup:
                    signals.append("NEUTRAL")
                    continue
                window = df.iloc[:i + 1]
                try:
                    sig = ind.generate_signal(window)
                    signals.append(sig.signal_type.value)
                except Exception:
                    signals.append("NEUTRAL")
            df[f"signal_{key}"] = signals

        return df

    def _precompute_arrays(self, df: pd.DataFrame, all_keys: List[str]) -> Dict:
        """
        將 DataFrame 中回測需要的欄位預提取為 numpy 陣列，
        避免在 run_single_combo 迴圈中反覆呼叫 df.iloc。
        """
        # 信號欄位轉成數字：BUY/STRONG_BUY=1, SELL/STRONG_SELL=-1, 其他=0
        buy_set = {"BUY", "STRONG_BUY"}
        sell_set = {"SELL", "STRONG_SELL"}

        signal_buy = {}   # key -> np.array of bool
        signal_sell = {}  # key -> np.array of bool
        for k in all_keys:
            col = f"signal_{k}"
            vals = df[col].values
            signal_buy[k] = np.array([v in buy_set for v in vals])
            signal_sell[k] = np.array([v in sell_set for v in vals])

        return {
            "open": df['open'].values.astype(float),
            "close": df['close'].values.astype(float),
            "index_str": [str(d) for d in df.index],
            "n": len(df),
            "signal_buy": signal_buy,
            "signal_sell": signal_sell,
        }

    def run_single_combo(
        self,
        arrays: Dict,
        combo_keys: Tuple[str, ...],
    ) -> Dict:
        """
        Phase 2：對一組指標組合執行回測（純 numpy，無 pandas）。

        買入條件：combo 中所有指標在前一天都是 BUY 或 STRONG_BUY
        賣出條件：combo 中任一指標在前一天是 SELL 或 STRONG_SELL
        執行價：隔日開盤價（避免 look-ahead bias）
        """
        trades = []
        position = None
        capital = self.initial_capital
        peak_capital = capital
        max_drawdown = 0.0

        open_arr = arrays["open"]
        close_arr = arrays["close"]
        idx_str = arrays["index_str"]
        n = arrays["n"]

        # 預算每天的 combo 聯合信號
        combo_all_buy = arrays["signal_buy"][combo_keys[0]].copy()
        for k in combo_keys[1:]:
            combo_all_buy &= arrays["signal_buy"][k]

        combo_any_sell = arrays["signal_sell"][combo_keys[0]].copy()
        for k in combo_keys[1:]:
            combo_any_sell |= arrays["signal_sell"][k]

        slippage_buy = 1 + self.slippage
        slippage_sell = 1 - self.slippage
        comm_rate = self.commission_rate
        tax_rate = self.tax_rate

        for i in range(self.warmup_period + 1, n - 1):
            # 前一天的信號 -> 隔日（i+1）開盤執行
            prev_buy = combo_all_buy[i - 1]
            prev_sell = combo_any_sell[i - 1]

            if position is None and prev_buy:
                price = open_arr[i + 1] * slippage_buy
                shares = int(capital * 0.95 / (price * (1 + comm_rate)))
                if shares > 0:
                    cost = shares * price * (1 + comm_rate)
                    capital -= cost
                    position = {
                        "entry_idx": i + 1,
                        "entry_price": round(price, 2),
                        "shares": shares,
                    }

            elif position is not None and prev_sell:
                price = open_arr[i + 1] * slippage_sell
                revenue = position["shares"] * price
                net = revenue * (1 - comm_rate - tax_rate)
                capital += net

                entry_cost = position["shares"] * position["entry_price"]
                pnl = net - entry_cost
                pnl_pct = pnl / entry_cost * 100

                ei = position["entry_idx"]
                trades.append({
                    "entry_date": idx_str[ei],
                    "exit_date": idx_str[i + 1],
                    "entry_price": position["entry_price"],
                    "exit_price": round(price, 2),
                    "shares": position["shares"],
                    "pnl": round(pnl),
                    "pnl_pct": round(pnl_pct, 2),
                    "hold_days": self._date_diff(idx_str[ei], idx_str[i + 1]),
                })
                position = None

            # 追蹤最大回撤
            equity = capital
            if position is not None:
                equity += position["shares"] * close_arr[i]
            if equity > peak_capital:
                peak_capital = equity
            if peak_capital > 0:
                dd = (peak_capital - equity) / peak_capital * 100
                if dd > max_drawdown:
                    max_drawdown = dd

        # 回測結束仍持有 → 最後收盤價強制平倉
        if position is not None:
            last_price = close_arr[-1]
            revenue = position["shares"] * last_price
            net = revenue * (1 - comm_rate - tax_rate)
            capital += net

            entry_cost = position["shares"] * position["entry_price"]
            pnl = net - entry_cost
            pnl_pct = pnl / entry_cost * 100

            ei = position["entry_idx"]
            trades.append({
                "entry_date": idx_str[ei],
                "exit_date": idx_str[-1],
                "entry_price": position["entry_price"],
                "exit_price": round(last_price, 2),
                "shares": position["shares"],
                "pnl": round(pnl),
                "pnl_pct": round(pnl_pct, 2),
                "hold_days": self._date_diff(idx_str[ei], idx_str[-1]),
                "forced_close": True,
            })

        return self._calc_metrics(trades, capital, max_drawdown, n)

    @staticmethod
    def _date_diff(d1: str, d2: str) -> int:
        try:
            return (pd.Timestamp(d2) - pd.Timestamp(d1)).days
        except Exception:
            return 0

    def _calc_metrics(
        self,
        trades: List[Dict],
        final_capital: float,
        max_drawdown: float,
        total_days: int,
    ) -> Dict:
        """計算績效指標"""
        total_return = (final_capital - self.initial_capital) / self.initial_capital * 100

        if not trades:
            return {
                "total_return_pct": round(total_return, 2),
                "annual_return_pct": 0,
                "max_drawdown_pct": 0,
                "win_rate": 0,
                "trade_count": 0,
                "avg_hold_days": 0,
                "sharpe_ratio": 0,
                "profit_loss_ratio": 0,
                "trades": [],
            }

        pnl_list = [t["pnl_pct"] for t in trades]
        wins = [p for p in pnl_list if p > 0]
        losses = [p for p in pnl_list if p <= 0]

        win_rate = len(wins) / len(trades) * 100 if trades else 0
        avg_hold = np.mean([t["hold_days"] for t in trades]) if trades else 0

        # 年化報酬率
        effective_days = max(total_days - self.warmup_period, 1)
        years = effective_days / 252
        if years > 0 and total_return > -100:
            annual_return = ((1 + total_return / 100) ** (1 / years) - 1) * 100
        else:
            annual_return = total_return

        # 夏普比率（假設無風險利率 0）
        if len(pnl_list) > 1:
            sharpe = np.mean(pnl_list) / np.std(pnl_list) if np.std(pnl_list) > 0 else 0
        else:
            sharpe = 0

        # 盈虧比
        avg_win = np.mean(wins) if wins else 0
        avg_loss = abs(np.mean(losses)) if losses else 0
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else (999.99 if avg_win > 0 else 0)

        return {
            "total_return_pct": float(round(total_return, 2)),
            "annual_return_pct": float(round(annual_return, 2)),
            "max_drawdown_pct": float(round(max_drawdown, 2)),
            "win_rate": float(round(win_rate, 1)),
            "trade_count": len(trades),
            "avg_hold_days": float(round(avg_hold, 1)),
            "sharpe_ratio": float(round(sharpe, 2)),
            "profit_loss_ratio": float(round(profit_loss_ratio, 2)),
            "trades": trades,
        }

    def run_all_combos_with_progress(
        self,
        df: pd.DataFrame,
        indicator_keys: List[str],
        min_combo: int = 2,
        max_combo: int = 3,
        progress_callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        Phase 3：排列組合所有指標，批量回測，帶進度回報。
        """
        # 預提取 numpy 陣列（只做一次）
        arrays = self._precompute_arrays(df, indicator_keys)

        # 列出所有組合
        all_combos = []
        for size in range(min_combo, max_combo + 1):
            for combo in combinations(indicator_keys, size):
                all_combos.append(combo)

        total = len(all_combos)
        results = []

        for idx, combo in enumerate(all_combos):
            result = self.run_single_combo(arrays, combo)
            result["combo"] = list(combo)
            result["combo_display"] = [INDICATOR_DISPLAY.get(k, k) for k in combo]
            result["combo_size"] = len(combo)
            results.append(result)

            if progress_callback and total > 0:
                pct = int((idx + 1) / total * 100)
                progress_callback(pct, idx + 1, total)

        # 依總報酬率排序
        results.sort(key=lambda x: x["total_return_pct"], reverse=True)

        # 加上排名，只保留前 100 名的交易明細以減少 JSON 體積
        for i, r in enumerate(results):
            r["rank"] = i + 1
            if i >= 100:
                r["trades"] = []

        return results
