# 回測系統規劃書（Backtesting System Specification）

> **目的**：在 crypto-signal-pro 專案中新增一個獨立頁面「回測中心」，讓使用者可以輸入股票代碼，對系統現有的所有技術指標進行排列組合回測，找出報酬率最高的組合。
>
> **讀者**：此文件寫給另一個 AI（Gemini 2.5 Pro）閱讀並實作。文件包含系統全局架構、現有指標清單、資料流、前後端慣例、以及回測系統的具體設計規格。

---

## 一、系統全局架構

### 1.1 專案結構

```
crypto-signal-pro/
├── backend/
│   ├── main.py                        # FastAPI 主程式（所有 API endpoint）
│   ├── screener.py                    # 超級選股系統（評分 + 分類）
│   ├── indicators/                    # 技術指標插件目錄
│   │   ├── base.py                    # BaseIndicator 抽象類別
│   │   ├── registry.py                # @register_indicator 裝飾器
│   │   ├── rsi.py                     # RSI 指標
│   │   ├── macd.py                    # MACD 指標
│   │   ├── bollinger.py               # 布林通道
│   │   ├── mfi.py                     # MFI 資金流量指標
│   │   ├── ema.py                     # EMA 均線交叉
│   │   ├── volume.py                  # 成交量 + OBV
│   │   ├── adx.py                     # ADX 趨勢強度
│   │   ├── stoch_rsi.py               # 隨機 RSI
│   │   ├── volume_reversal.py         # 爆量反轉
│   │   └── pullback_support.py        # 均線拉回支撐
│   ├── signals/
│   │   └── aggregator.py              # SignalAggregator 信號聚合器
│   ├── layers/                        # 分析層（修正因子，非純技術指標）
│   │   ├── regime.py                  # 盤勢判斷（多頭/空頭/盤整...）
│   │   ├── chipflow.py                # 籌碼面（法人買賣超）
│   │   ├── fundamental.py             # 基本面（P/E、PEG）
│   │   ├── sentiment.py               # 消息面（RSS 新聞）
│   │   └── active_etf.py              # 主動式 ETF 持股
│   └── data/
│       └── history/stock/             # 本地 OHLCV CSV 快取
├── frontend/
│   ├── index.html                     # 分析儀表板
│   ├── trading.html                   # 幣圈交易中心
│   ├── sector_trading.html            # 台股交易中心
│   ├── signal_performance.html        # 信號績效統計
│   ├── settings.html                  # 後台設定
│   ├── app.js                         # 主頁邏輯
│   └── style.css                      # 多主題 CSS
```

### 1.2 技術棧

- **後端**：Python 3.11+ / FastAPI / Uvicorn
- **前端**：純 Vanilla JavaScript（無框架）、CSS Variables 主題系統
- **圖表**：Chart.js（統計圖）、TradingView Lightweight Charts（K 線圖）
- **資料來源**：yfinance（OHLCV 歷史價格）、TWSE OpenAPI（法人/融資券/基本面）、FinMind API
- **部署**：本地開發，FastAPI 掛載 `/dashboard` 提供前端靜態檔

### 1.3 OHLCV 資料格式

系統內所有 DataFrame 都遵循此格式：

```python
# 欄位：open, high, low, close, volume
# Index：pd.DatetimeIndex
# 日期範圍：yfinance 預設抓 1 年日線（約 250 筆）
# 本地快取位置：backend/data/history/stock/{symbol}.csv
#   例如 2330.TW → backend/data/history/stock/2330_TW.csv
```

**取得歷史資料的方式（回測時請直接用 yfinance，不要走 main.py 的 rate limiter）**：

```python
import yfinance as yf

def fetch_ohlcv(symbol: str, period: str = "2y") -> pd.DataFrame:
    """
    回測專用：直接從 yfinance 抓取歷史資料
    period 可用 "1y", "2y", "5y", "max"
    """
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval="1d")
    df.columns = [c.lower() for c in df.columns]
    # yfinance 回傳的欄位：Open, High, Low, Close, Volume, Dividends, Stock Splits
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df.index = pd.DatetimeIndex(df.index)
    return df
```

> **注意**：yfinance 對台股（`.TW` 結尾）有速率限制，批量回測時請加 `time.sleep(1)` 間隔。

---

## 二、現有技術指標完整清單

以下是系統中 **所有已實作的技術指標**，每個都是 `backend/indicators/` 目錄下的獨立插件。

### 2.1 指標插件架構

每個指標必須繼承 `BaseIndicator`，實作兩個方法：

```python
# backend/indicators/base.py

class BaseIndicator(ABC):
    def __init__(self, name: str, max_score: float = 15.0, params: dict = None):
        self.name = name
        self.max_score = max_score
        self.params = params or {}

    @abstractmethod
    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """在 DataFrame 上新增指標欄位（如 'rsi_14', 'macd' 等）"""
        pass

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> IndicatorSignal:
        """根據最新一筆資料產生 BUY / SELL / NEUTRAL 信號"""
        pass
```

信號類型：

```python
class SignalType(Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"
```

### 2.2 十大已實作指標

| # | 指標名稱 | registry key | 檔案 | 計算內容 | 主要參數 | 買入條件 | 賣出條件 |
|---|---------|-------------|------|---------|---------|---------|---------|
| 1 | **RSI** | `rsi` | `rsi.py` | 14 期 Wilder RSI | period=14, oversold=30, overbought=70 | RSI < 30（超賣） | RSI > 70（超買） |
| 2 | **MACD** | `macd` | `macd.py` | 12/26 EMA 差 + 9 期信號線 | fast=12, slow=26, signal=9 | 金叉（MACD 上穿信號線） | 死叉（MACD 下穿信號線） |
| 3 | **Bollinger Bands** | `bollinger` | `bollinger.py` | 20 期 SMA ± 2 標準差 | period=20, std_dev=2.0 | %B < 0.2（接近下軌） | %B > 0.8（接近上軌） |
| 4 | **MFI** | `mfi` | `mfi.py` | 14 期資金流量指標 | period=14, oversold=20, overbought=80 | MFI < 20 | MFI > 80 |
| 5 | **EMA Cross** | `ema_cross` | `ema.py` | EMA9/21 短交叉 + EMA50/200 長交叉 | fast=9, slow=21, long_fast=50, long_slow=200 | 金叉（9>21 或 50>200） | 死叉（9<21 或 50<200） |
| 6 | **Volume** | `volume` | `volume.py` | 成交量 vs 20MA + OBV 趨勢 | ma_period=20, spike_mult=2.0 | 量增 ≥ 1.5x + 紅K | 量增 ≥ 1.5x + 黑K |
| 7 | **ADX** | `adx` | `adx.py` | 14 期 ADX + +DI/-DI | period=14, trend_threshold=25 | ADX>25 且 +DI > -DI | ADX>25 且 -DI > +DI |
| 8 | **Stochastic RSI** | `stoch_rsi` | `stoch_rsi.py` | RSI 的隨機振盪器 | rsi=14, stoch=14, K=3, D=3 | K < 0.2 且上穿 D | K > 0.8 且下穿 D |
| 9 | **Volume Reversal** | `volume_reversal` | `volume_reversal.py` | 20 日高低點 + 爆量反轉偵測 | lookback=20, reversal_vol=2.5x | 近低點 + 爆量 + 紅K | 近高點 + 爆量 + 黑K |
| 10 | **Pullback Support** | `pullback_support` | `pullback_support.py` | EMA21/50/200 支撐 + 縮量 + RSI 保護 | near_pct=2.5%, rsi_floor=35 | 拉回 EMA21 + 縮量 | 跌破 EMA200 3% |

### 2.3 回測應新增的候選指標（目前系統未實作）

以下指標在台股常見但系統尚未實作，**請在回測系統中新增計算**，讓回測數據決定它們是否有用：

| # | 指標名稱 | 計算方式 | 回測用途 |
|---|---------|---------|---------|
| 11 | **乖離率 (BIAS)** | `(close - MA_N) / MA_N × 100`，建議測試 MA5/10/20/60 | 均值回歸信號：正乖離過大→超買，負乖離過大→超賣。門檻依 MA 週期不同（例如 BIAS_20 > 8% 超買、< -8% 超賣） |
| 12 | **KD 指標 (Stochastic)** | 9 期 K/D 值（注意：和 Stochastic RSI 不同，這是原始 Stochastic） | 經典台股指標，KD 金叉/死叉 |
| 13 | **DMI (方向移動指標)** | 和 ADX 相關但更細緻，+DI/-DI 交叉 | 可與 ADX 組合測試 |
| 14 | **VWAP (量加權均價)** | `cumsum(typical_price × volume) / cumsum(volume)`，日內重算 | 短線支撐壓力 |
| 15 | **威廉指標 (Williams %R)** | `(highest_high - close) / (highest_high - lowest_low) × -100`，14 期 | 類似 RSI 但更敏感 |

> **關於乖離率**：它本質上是均線偏離度，在盤整市場有不錯的回歸效果，但在強趨勢中會持續偏離不回歸。放入回測讓數據說話即可。

---

## 三、回測系統設計規格

### 3.1 回測核心邏輯

```
輸入：
  - symbol（股票代碼，如 "2330.TW"）
  - period（回測期間，如 "2y"）
  - 初始資金（預設 100 萬）
  - 手續費率（預設 0.1425%，台股券商折扣後約 0.06%）
  - 交易稅（預設 0.3%，賣出時收）
  - 滑價（預設 0，可選 0.05%）

輸出：
  - 每種「指標組合」的績效指標：
    - 總報酬率（%）
    - 年化報酬率（%）
    - 最大回撤（%）
    - 勝率（%）= 獲利交易數 / 總交易數
    - 交易次數
    - 平均持有天數
    - 夏普比率 (Sharpe Ratio)
    - 盈虧比 = 平均獲利 / 平均虧損
  - 依總報酬率排序的排行榜
  - 每組合的交易明細（可展開查看）
```

### 3.2 「指標組合」的定義

一個「指標組合」= 從上述 10~15 個指標中選擇 **2~4 個**，全部同時給出買入信號時才買入。

```
買入規則：所選指標「全部」同時為 BUY 或 STRONG_BUY → 隔日開盤買入
賣出規則：所選指標「任一」為 SELL 或 STRONG_SELL → 隔日開盤賣出

（注意：用隔日開盤價而非當日收盤價，這更接近真實可執行的情況）
```

**組合數量控制**：
- 從 N 個指標中選 2 個：C(N,2)
- 從 N 個指標中選 3 個：C(N,3)
- 從 N 個指標中選 4 個：C(N,4)
- 10 個指標時：45 + 120 + 210 = **375 種組合**
- 15 個指標時：105 + 455 + 1365 = **1925 種組合**

這個量級在 Python 內是可接受的（每個組合跑一次回測約 0.01~0.05 秒）。

### 3.3 回測引擎設計

```python
# 建議新增檔案：backend/backtest_engine.py

import pandas as pd
import numpy as np
from itertools import combinations
from typing import List, Dict, Tuple

class BacktestEngine:
    """
    技術指標組合回測引擎
    """

    def __init__(
        self,
        initial_capital: float = 1_000_000,
        commission_rate: float = 0.001425,   # 券商手續費 0.1425%（可改）
        tax_rate: float = 0.003,             # 證交稅 0.3%（賣出時）
        slippage: float = 0.0,               # 滑價（預設 0）
    ):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.tax_rate = tax_rate
        self.slippage = slippage

    def prepare_signals(
        self,
        df: pd.DataFrame,
        indicators: List[BaseIndicator],
    ) -> pd.DataFrame:
        """
        Phase 1：在 OHLCV 上計算所有指標，並為每個指標標記每日信號
        
        回傳 DataFrame 包含原始 OHLCV + 每個指標的信號欄位
        欄位命名：signal_{indicator_key}，值為 "BUY"/"SELL"/"NEUTRAL"
        """
        # 1. 計算所有指標欄位
        for ind in indicators:
            df = ind.calculate(df)
        
        # 2. 逐日產生信號（不能只看最後一天，要逐日回溯）
        #    重要：generate_signal() 通常只看最後一筆
        #    回測時需要用滾動窗口逐日呼叫
        for ind in indicators:
            key = ... # 從 INDICATOR_NAME_TO_KEY 反查
            signals = []
            for i in range(len(df)):
                if i < 200:  # 前 200 天資料不足，標記 NEUTRAL
                    signals.append("NEUTRAL")
                    continue
                window = df.iloc[:i+1]
                sig = ind.generate_signal(window)
                signals.append(sig.signal_type.value)
            df[f"signal_{key}"] = signals

        return df

    def run_single_combo(
        self,
        df: pd.DataFrame,
        combo_keys: Tuple[str, ...],
    ) -> Dict:
        """
        Phase 2：對一組指標組合執行回測
        
        買入條件：combo 中所有指標在同一天都是 BUY 或 STRONG_BUY
        賣出條件：combo 中任一指標是 SELL 或 STRONG_SELL
        執行價：隔日開盤價
        """
        trades = []
        position = None  # None 或 {"entry_date", "entry_price", "shares"}
        capital = self.initial_capital

        for i in range(1, len(df) - 1):  # 從第 2 天開始（需要前一天信號）
            prev = df.iloc[i - 1]
            today = df.iloc[i]
            next_open = df.iloc[i + 1]['open']  # 隔日開盤價

            # 讀取前一天的信號（信號在收盤後產生，隔日才能執行）
            buy_signals = [
                prev[f"signal_{k}"] in ("BUY", "STRONG_BUY")
                for k in combo_keys
            ]
            sell_signals = [
                prev[f"signal_{k}"] in ("SELL", "STRONG_SELL")
                for k in combo_keys
            ]

            if position is None and all(buy_signals):
                # 買入
                price = next_open * (1 + self.slippage)
                commission = price * self.commission_rate
                shares = int(capital * 0.95 / (price + commission))  # 保留 5% 現金
                if shares > 0:
                    cost = shares * price + shares * commission
                    capital -= cost
                    position = {
                        "entry_date": df.index[i + 1],
                        "entry_price": price,
                        "shares": shares,
                    }

            elif position is not None and any(sell_signals):
                # 賣出
                price = next_open * (1 - self.slippage)
                revenue = position["shares"] * price
                commission = revenue * self.commission_rate
                tax = revenue * self.tax_rate
                net = revenue - commission - tax
                capital += net

                pnl = net - position["shares"] * position["entry_price"]
                pnl_pct = pnl / (position["shares"] * position["entry_price"]) * 100

                trades.append({
                    "entry_date": position["entry_date"],
                    "exit_date": df.index[i + 1],
                    "entry_price": position["entry_price"],
                    "exit_price": price,
                    "shares": position["shares"],
                    "pnl": round(pnl),
                    "pnl_pct": round(pnl_pct, 2),
                    "hold_days": (df.index[i + 1] - position["entry_date"]).days,
                })
                position = None

        # 如果回測結束還持有，以最後一天收盤價強制平倉
        if position is not None:
            last_price = df.iloc[-1]['close']
            revenue = position["shares"] * last_price
            commission = revenue * self.commission_rate
            tax = revenue * self.tax_rate
            capital += revenue - commission - tax
            pnl = (revenue - commission - tax) - position["shares"] * position["entry_price"]
            trades.append({
                "entry_date": position["entry_date"],
                "exit_date": df.index[-1],
                "entry_price": position["entry_price"],
                "exit_price": last_price,
                "shares": position["shares"],
                "pnl": round(pnl),
                "pnl_pct": round(pnl / (position["shares"] * position["entry_price"]) * 100, 2),
                "hold_days": (df.index[-1] - position["entry_date"]).days,
                "forced_close": True,
            })

        return self._calc_metrics(trades, capital)

    def _calc_metrics(self, trades, final_capital):
        """計算績效指標"""
        total_return = (final_capital - self.initial_capital) / self.initial_capital * 100
        # ... 計算年化報酬、最大回撤、夏普比率等
        return {
            "total_return_pct": round(total_return, 2),
            "trade_count": len(trades),
            "win_rate": ...,
            "max_drawdown": ...,
            "sharpe_ratio": ...,
            "avg_hold_days": ...,
            "profit_loss_ratio": ...,
            "trades": trades,
        }

    def run_all_combos(
        self,
        df: pd.DataFrame,
        indicator_keys: List[str],
        min_combo: int = 2,
        max_combo: int = 4,
    ) -> List[Dict]:
        """
        Phase 3：排列組合所有指標，批量回測
        """
        results = []
        for size in range(min_combo, max_combo + 1):
            for combo in combinations(indicator_keys, size):
                result = self.run_single_combo(df, combo)
                result["combo"] = list(combo)
                result["combo_size"] = size
                results.append(result)

        # 依總報酬率排序
        results.sort(key=lambda x: x["total_return_pct"], reverse=True)
        return results
```

### 3.4 重要實作注意事項

#### 3.4.1 逐日信號產生（最關鍵的一點）

現有指標的 `generate_signal(df)` 只看 DataFrame 最後一筆。回測時**不能**把整個 2 年資料丟進去只得到 1 個信號。必須用滾動窗口：

```python
# ❌ 錯誤：只得到最後一天的信號
signal = indicator.generate_signal(full_df)

# ✅ 正確：逐日產生信號
for i in range(warmup, len(full_df)):
    window = full_df.iloc[:i+1]
    signal = indicator.generate_signal(window)
    df.loc[df.index[i], f"signal_{key}"] = signal.signal_type.value
```

#### 3.4.2 暖機期 (Warmup Period)

- EMA200 需要至少 200 筆資料才有意義
- 因此回測有效期間 = 資料總長 - 200 天
- 若用 `period="2y"` 約 500 筆，有效回測天數約 300 天
- 若想要更長的回測期間，可用 `period="5y"` 或 `period="max"`

#### 3.4.3 避免未來偷看 (Look-Ahead Bias)

- 信號在**當天收盤後**產生，執行價用**隔日開盤價**
- 不能用當天的收盤價買入（因為信號是收盤後才知道的）
- 這已在上面的 `run_single_combo()` 中處理（`prev` 信號 → `next_open` 執行）

#### 3.4.4 效能優化

指標計算（`calculate()`）只需要做一次，信號產生（`generate_signal()`）需要逐日。建議：

```python
# Phase 1：一次性計算所有指標欄位（快，向量化操作）
for ind in all_indicators:
    df = ind.calculate(df)

# Phase 2：逐日產生信號（慢，但只需做一次）
#   把每個指標每天的信號存成 DataFrame 欄位
#   → signal_rsi, signal_macd, signal_ema_cross, ...

# Phase 3：組合回測只需讀取 Phase 2 的信號欄位
#   → 純 DataFrame 布林運算，非常快
for combo in all_combos:
    mask_buy = df[f"signal_{combo[0]}"].isin(["BUY","STRONG_BUY"])
    for key in combo[1:]:
        mask_buy &= df[f"signal_{key}"].isin(["BUY","STRONG_BUY"])
    # ... 遍歷執行交易
```

這樣 Phase 3 的每個組合回測就是純 DataFrame 欄位讀取 + 迴圈遍歷，而不需要重新計算指標。

---

## 四、API 端點設計

### 4.1 新增端點

在 `backend/main.py` 中新增：

```python
# ── 回測 API ──

@app.post("/api/backtest/run")
async def run_backtest(payload: dict):
    """
    觸發回測（背景執行）
    
    Request body:
    {
        "symbol": "2330.TW",
        "period": "2y",               # "1y" / "2y" / "5y"
        "initial_capital": 1000000,    # 選填，預設 100 萬
        "commission_rate": 0.001425,   # 選填
        "tax_rate": 0.003,             # 選填
        "min_combo_size": 2,           # 選填，最小組合數
        "max_combo_size": 4,           # 選填，最大組合數
        "include_new_indicators": true # 選填，是否包含新候選指標（乖離率、KD等）
    }
    
    Response:
    {
        "status": "started",
        "task_id": "bt_2330_20260423_abc123"
    }
    """

@app.get("/api/backtest/status/{task_id}")
async def get_backtest_status(task_id: str):
    """
    查詢回測進度
    
    Response:
    {
        "status": "running" / "completed" / "failed",
        "progress": 45,            # 0-100 百分比
        "total_combos": 375,
        "completed_combos": 170,
        "estimated_remaining_sec": 12
    }
    """

@app.get("/api/backtest/result/{task_id}")
async def get_backtest_result(task_id: str):
    """
    取得回測結果
    
    Response:
    {
        "symbol": "2330.TW",
        "period": "2y",
        "total_combos": 375,
        "buy_and_hold_return": 23.5,  # 同期買入持有報酬率（基準）
        "results": [
            {
                "rank": 1,
                "combo": ["ema_cross", "adx", "macd"],
                "combo_display": ["EMA均線交叉", "ADX趨勢", "MACD"],
                "total_return_pct": 45.2,
                "annual_return_pct": 22.1,
                "max_drawdown_pct": -12.3,
                "win_rate": 68.5,
                "trade_count": 15,
                "avg_hold_days": 18,
                "sharpe_ratio": 1.45,
                "profit_loss_ratio": 2.1,
                "trades": [...]  # 可選，前端展開時才載入
            },
            ...
        ]
    }
    """

@app.get("/api/backtest/history")
async def get_backtest_history():
    """
    取得過去的回測紀錄列表（簡要）
    
    Response:
    {
        "records": [
            {
                "task_id": "bt_2330_20260423_abc123",
                "symbol": "2330.TW",
                "period": "2y",
                "created_at": "2026-04-23 15:30",
                "status": "completed",
                "best_combo": ["ema_cross", "adx"],
                "best_return": 45.2
            }
        ]
    }
    """
```

### 4.2 回測結果持久化

```python
# 建議存放位置：backend/data/backtest/
#   bt_2330_20260423_abc123.json  ← 完整結果
#   backtest_index.json           ← 歷史索引

# backtest_index.json 格式：
{
    "records": [
        {
            "task_id": "bt_2330_20260423_abc123",
            "symbol": "2330.TW",
            "period": "2y",
            "created_at": "2026-04-23 15:30:00",
            "status": "completed",
            "total_combos": 375,
            "best_combo": ["ema_cross", "adx"],
            "best_return_pct": 45.2,
            "buy_hold_return_pct": 23.5
        }
    ]
}
```

---

## 五、前端頁面設計

### 5.1 新增頁面：`frontend/backtest.html`

#### 導覽列

在所有頁面的 sidebar 中加入連結：

```html
<!-- 在 settings.html 連結之前加入 -->
<a href="backtest.html" class="nav-item">📊 回測中心</a>
```

#### 頁面佈局

```
┌─────────────────────────────────────────────────────┐
│  📊 回測中心                                          │
├─────────────────────────────────────────────────────┤
│                                                       │
│  [股票代碼輸入框]  [回測期間下拉]  [開始回測 按鈕]      │
│                                                       │
│  ┌──────── 進度條（回測中才顯示）────────┐             │
│  │ ████████░░░░░  45% (170/375)  剩餘12s │             │
│  └───────────────────────────────────────┘             │
│                                                       │
│  ── 回測結果排行榜 ──                                  │
│  基準：買入持有報酬率 +23.5%                            │
│                                                       │
│  # │ 指標組合                  │ 報酬率  │ 勝率  │ ... │
│  1 │ EMA交叉 + ADX + MACD     │ +45.2% │ 68.5% │     │
│  2 │ EMA交叉 + ADX            │ +38.7% │ 71.2% │     │
│  3 │ MACD + RSI + Volume      │ +32.1% │ 55.0% │     │
│  ...                                                  │
│                                                       │
│  [點擊展開] 交易明細                                   │
│  ┌─────────────────────────────────────┐              │
│  │ 買入日期 │ 賣出日期 │ 持有 │ 損益    │              │
│  │ 2024/3/5 │ 2024/4/2 │ 28天 │ +8.2%  │              │
│  │ ...                                  │              │
│  └─────────────────────────────────────┘              │
│                                                       │
│  ── 歷史回測紀錄 ──                                    │
│  │ 2330.TW │ 2y │ 2026-04-23 │ 最佳: EMA+ADX +45.2%  │
│  │ 2454.TW │ 2y │ 2026-04-22 │ 最佳: MACD+RSI +33.1% │
│                                                       │
└─────────────────────────────────────────────────────┘
```

### 5.2 前端慣例（務必遵守）

1. **CSS 類別**：使用現有 `style.css` 的類別：
   - 卡片容器：`class="glass-panel"`
   - 按鈕：`class="btn-primary"`
   - 表格：參考 `signal_performance.html` 的 `.stock-table` 樣式
   - 狀態色：用 CSS 變數 `var(--buy-color)` 表示正報酬（綠色）、`var(--sell-color)` 表示負報酬（紅色）

2. **主題相容**：所有顏色必須用 CSS 變數，不能寫死色碼。系統有 4 個主題：
   - `kawaii`（預設粉色系）
   - `pro-dark`（深色專業）
   - `ocean`（藍色海洋）
   - `warm`（暖色護眼）

3. **主題初始化**（每個 HTML 頁面都必須有）：
   ```html
   <script>
       (function() {
           var t = localStorage.getItem('csp-theme') || 'kawaii';
           document.documentElement.setAttribute('data-theme', t);
       })();
   </script>
   ```

4. **API 呼叫模式**：
   ```javascript
   // GET
   const res = await fetch('/api/backtest/result/bt_2330_xxx');
   const data = await res.json();

   // POST
   const res = await fetch('/api/backtest/run', {
       method: 'POST',
       headers: { 'Content-Type': 'application/json' },
       body: JSON.stringify({ symbol: '2330.TW', period: '2y' })
   });
   ```

5. **數字格式**：
   - 報酬率正值顯示綠色 `+45.2%`，負值紅色 `-12.3%`
   - 金額用千分位：`1,234,567`
   - 勝率用百分比：`68.5%`

6. **語言**：所有使用者可見文字用**繁體中文**。

---

## 六、指標的 registry key 對照表

回測時需要用 key 來識別指標，以下是完整對照：

```python
# 現有指標
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
}

# 新增候選指標（需要在回測系統中新增實作）
NEW_INDICATOR_DISPLAY = {
    "bias":             "乖離率 (BIAS)",
    "kd":               "KD 隨機指標",
    "williams_r":       "威廉指標 %R",
}
```

---

## 七、現有指標的信號判斷邏輯摘要

回測時需要逐日判斷每個指標是 BUY/SELL/NEUTRAL。以下是各指標的關鍵判斷邏輯（簡化版），用於回測引擎中的 `generate_signal()` 逐日呼叫：

```python
# RSI (rsi.py)
if rsi < 20:        STRONG_BUY
elif rsi < 30:      BUY
elif 35 <= rsi <= 40 and rsi_rising:  BUY  # RSI 回升中
elif rsi > 80:      STRONG_SELL
elif rsi > 70:      SELL
else:               NEUTRAL

# MACD (macd.py)
if macd > signal and prev_macd <= prev_signal:  STRONG_BUY  # 金叉
elif histogram > 0 and histogram > prev_histogram:  BUY     # 柱狀圖擴大
elif macd < signal and prev_macd >= prev_signal:  STRONG_SELL  # 死叉
elif histogram < 0 and histogram < prev_histogram:  SELL
else:               NEUTRAL

# EMA Cross (ema.py)
if ema50 > ema200:  long_bullish = True   # 長期多頭
if ema9 > ema21:    short_bullish = True  # 短期多頭
# STRONG_BUY: 長期金叉剛發生
# BUY: 短期金叉 或 多頭排列（9>21>50>200）
# STRONG_SELL: 長期死叉
# SELL: 短期死叉

# ADX (adx.py)
if adx > 40 and plus_di > minus_di:   STRONG_BUY  # 強勢上升趨勢
elif adx > 25 and plus_di > minus_di:  BUY         # 上升趨勢確認
elif adx > 40 and minus_di > plus_di:  STRONG_SELL
elif adx > 25 and minus_di > plus_di:  SELL
else:               NEUTRAL  # ADX < 25 = 無趨勢

# Volume (volume.py)
vol_ratio = volume / volume_sma20
bullish_candle = close > open
if vol_ratio >= 2.0 and bullish_candle:  STRONG_BUY   # 爆量紅K
elif vol_ratio >= 1.5 and bullish_candle:  BUY
elif vol_ratio >= 2.0 and not bullish_candle:  STRONG_SELL
elif vol_ratio >= 1.5 and not bullish_candle:  SELL
else:               NEUTRAL

# Stoch RSI (stoch_rsi.py)
if k < 0.20 and k > d and prev_k <= prev_d:  STRONG_BUY  # 超賣區金叉
elif k < 0.20:  BUY
elif k > 0.80 and k < d and prev_k >= prev_d:  STRONG_SELL  # 超買區死叉
elif k > 0.80:  SELL
else:               NEUTRAL

# Bollinger Bands (bollinger.py)
pct_b = (close - lower) / (upper - lower)
if pct_b <= 0:    STRONG_BUY   # 跌破下軌
elif pct_b < 0.2: BUY
elif pct_b >= 1:  STRONG_SELL  # 突破上軌
elif pct_b > 0.8: SELL
else:             NEUTRAL

# MFI (mfi.py)
if mfi < 10:  STRONG_BUY
elif mfi < 20:  BUY
elif mfi > 90:  STRONG_SELL
elif mfi > 80:  SELL
else:           NEUTRAL

# Volume Reversal (volume_reversal.py)
# 優先級最高：破底警告
if low < lowest_20d and vol_ratio >= 1.5:  STRONG_SELL（破底量，禁止做多）
# 其次：反轉信號
if near_20d_low and vol_ratio >= 2.5 and bullish:  STRONG_BUY
elif near_20d_low and vol_ratio >= 1.5 and bullish:  BUY
elif near_20d_high and vol_ratio >= 2.5 and bearish:  SELL

# Pullback Support (pullback_support.py)
# 優先級最高：跌破長期均線
if close < ema200 * 0.97:  STRONG_SELL（趨勢崩壞）
if close < ema200:  NEUTRAL（多頭未確認）
if rsi < 35:  NEUTRAL（跌太深，不是拉回而是崩跌）
# 支撐信號
if near_ema21 and vol_shrink:  STRONG_BUY（最佳進場點）
elif near_ema21:  BUY
elif near_ema50 and vol_shrink and rsi > 40:  BUY
elif near_ema50 and rsi > 40:  BUY
```

---

## 八、新增候選指標的實作規格

### 8.1 乖離率 (BIAS)

```python
# backend/indicators/bias.py（新檔案）

@register_indicator('bias')
class BiasIndicator(BaseIndicator):
    def __init__(self, max_score=12.0, params=None):
        defaults = {
            "periods": [5, 10, 20, 60],  # 計算多條均線的乖離率
            "primary_period": 20,         # 主要判斷用 20MA 乖離率
            "overbought": 8.0,            # 正乖離 > 8% 為超買
            "oversold": -8.0,             # 負乖離 < -8% 為超賣
        }
        super().__init__("BIAS", max_score, {**defaults, **(params or {})})

    def calculate(self, df):
        for p in self.params["periods"]:
            ma = df['close'].rolling(p).mean()
            df[f'bias_{p}'] = (df['close'] - ma) / ma * 100
        return df

    def generate_signal(self, df):
        bias = df[f'bias_{self.params["primary_period"]}'].iloc[-1]
        if bias < self.params["oversold"]:
            return IndicatorSignal("BIAS", SignalType.BUY, self.max_score * 0.7, bias, {}, f"乖離率 {bias:.1f}% 超賣")
        elif bias > self.params["overbought"]:
            return IndicatorSignal("BIAS", SignalType.SELL, self.max_score * 0.7, bias, {}, f"乖離率 {bias:.1f}% 超買")
        else:
            return IndicatorSignal("BIAS", SignalType.NEUTRAL, 0, bias, {}, f"乖離率 {bias:.1f}% 中性")
```

### 8.2 KD 隨機指標

```python
# backend/indicators/kd.py（新檔案）

@register_indicator('kd')
class KDIndicator(BaseIndicator):
    def __init__(self, max_score=12.0, params=None):
        defaults = {"period": 9, "smooth_k": 3, "smooth_d": 3, "oversold": 20, "overbought": 80}
        super().__init__("KD", max_score, {**defaults, **(params or {})})

    def calculate(self, df):
        p = self.params["period"]
        low_min = df['low'].rolling(p).min()
        high_max = df['high'].rolling(p).max()
        rsv = (df['close'] - low_min) / (high_max - low_min) * 100
        df['kd_k'] = rsv.ewm(span=self.params["smooth_k"], adjust=False).mean()
        df['kd_d'] = df['kd_k'].ewm(span=self.params["smooth_d"], adjust=False).mean()
        return df

    def generate_signal(self, df):
        k, d = df['kd_k'].iloc[-1], df['kd_d'].iloc[-1]
        prev_k, prev_d = df['kd_k'].iloc[-2], df['kd_d'].iloc[-2]
        if k < self.params["oversold"] and k > d and prev_k <= prev_d:
            return IndicatorSignal("KD", SignalType.STRONG_BUY, ...)
        elif k > self.params["overbought"] and k < d and prev_k >= prev_d:
            return IndicatorSignal("KD", SignalType.STRONG_SELL, ...)
        # ... 其他判斷
```

### 8.3 威廉指標 (Williams %R)

```python
# backend/indicators/williams_r.py（新檔案）

@register_indicator('williams_r')
class WilliamsRIndicator(BaseIndicator):
    def __init__(self, max_score=10.0, params=None):
        defaults = {"period": 14, "oversold": -80, "overbought": -20}
        super().__init__("Williams %R", max_score, {**defaults, **(params or {})})

    def calculate(self, df):
        p = self.params["period"]
        hh = df['high'].rolling(p).max()
        ll = df['low'].rolling(p).min()
        df['williams_r'] = (hh - df['close']) / (hh - ll) * -100
        return df

    def generate_signal(self, df):
        wr = df['williams_r'].iloc[-1]
        if wr < self.params["oversold"]:
            return IndicatorSignal("Williams %R", SignalType.BUY, ...)
        elif wr > self.params["overbought"]:
            return IndicatorSignal("Williams %R", SignalType.SELL, ...)
        # ...
```

---

## 九、背景任務管理

回測可能耗時 30 秒 ~ 2 分鐘，必須用 **背景任務** 方式執行：

```python
# backend/main.py 中的背景任務模式（參考現有的 screener refresh 做法）

import asyncio
from concurrent.futures import ThreadPoolExecutor

_backtest_executor = ThreadPoolExecutor(max_workers=2)
_backtest_tasks: Dict[str, Dict] = {}  # task_id → {"status", "progress", "result"}

@app.post("/api/backtest/run")
async def run_backtest(payload: dict):
    task_id = f"bt_{payload['symbol'].replace('.', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _backtest_tasks[task_id] = {"status": "running", "progress": 0}

    loop = asyncio.get_event_loop()
    loop.run_in_executor(
        _backtest_executor,
        _execute_backtest,  # 同步函式
        task_id,
        payload,
    )

    return {"status": "started", "task_id": task_id}


def _execute_backtest(task_id: str, payload: dict):
    """在背景執行緒中執行回測（同步）"""
    try:
        engine = BacktestEngine(
            initial_capital=payload.get("initial_capital", 1_000_000),
            commission_rate=payload.get("commission_rate", 0.001425),
            tax_rate=payload.get("tax_rate", 0.003),
        )

        # 1. 抓取資料
        df = fetch_ohlcv(payload["symbol"], payload.get("period", "2y"))

        # 2. 計算所有指標 + 逐日信號
        indicators = _create_all_indicators(payload.get("include_new_indicators", True))
        df = engine.prepare_signals(df, indicators)

        # 3. 跑所有組合（帶進度更新）
        results = engine.run_all_combos_with_progress(
            df,
            indicator_keys=[...],
            progress_callback=lambda p: _backtest_tasks[task_id].update({"progress": p})
        )

        # 4. 計算基準（買入持有報酬）
        buy_hold = (df.iloc[-1]['close'] / df.iloc[200]['close'] - 1) * 100

        # 5. 存結果
        result_data = {
            "symbol": payload["symbol"],
            "period": payload.get("period", "2y"),
            "total_combos": len(results),
            "buy_and_hold_return": round(buy_hold, 2),
            "results": results[:100],  # 只保留前 100 名
        }

        _backtest_tasks[task_id] = {"status": "completed", "progress": 100, "result": result_data}

        # 6. 持久化到檔案
        _save_backtest_result(task_id, result_data)

    except Exception as e:
        _backtest_tasks[task_id] = {"status": "failed", "error": str(e)}
```

---

## 十、總結：實作步驟 Checklist

### 後端

1. [ ] 建立 `backend/backtest_engine.py`
   - `BacktestEngine` 類別
   - `prepare_signals()` — 逐日產生所有指標信號
   - `run_single_combo()` — 單組合回測
   - `run_all_combos()` — 批量回測 + 排序
   - `_calc_metrics()` — 計算績效指標

2. [ ] 新增候選指標（選做，但建議加）
   - `backend/indicators/bias.py` — 乖離率
   - `backend/indicators/kd.py` — KD 隨機指標
   - `backend/indicators/williams_r.py` — 威廉指標

3. [ ] 在 `backend/main.py` 新增 4 個 API
   - `POST /api/backtest/run` — 觸發回測
   - `GET /api/backtest/status/{task_id}` — 查詢進度
   - `GET /api/backtest/result/{task_id}` — 取得結果
   - `GET /api/backtest/history` — 歷史紀錄

4. [ ] 回測結果持久化
   - `backend/data/backtest/` 目錄
   - `backtest_index.json` 索引檔

### 前端

5. [ ] 建立 `frontend/backtest.html` — 回測中心頁面
6. [ ] 建立 `frontend/backtest.js`（或 inline script）
   - 輸入表單（股票代碼 + 期間 + 參數）
   - 進度條（polling `/api/backtest/status/`）
   - 結果排行榜表格
   - 交易明細展開/收合
   - 歷史紀錄列表
7. [ ] 在所有頁面的 sidebar 加入「回測中心」連結
8. [ ] 確保支援 4 個主題（用 CSS 變數）

### 驗證

9. [ ] 用 2330.TW（台積電）跑 2 年回測，確認：
   - 所有組合都有結果
   - 買入持有基準正確
   - 報酬率排序正確
   - 交易明細的進出場價格合理（隔日開盤價）
   - 手續費和交易稅有正確扣除
10. [ ] 用小型股（如 2395.TW 研華）測試，確認資料量不足時不會 crash

---

## 附錄 A：現有 sidebar 導覽結構

```html
<nav class="sidebar-nav">
    <a href="index.html" class="nav-item">分析儀表板</a>
    <a href="trading.html" class="nav-item">幣圈交易中心</a>
    <a href="sector_trading.html" class="nav-item">台股交易中心</a>
    <a href="signal_performance.html" class="nav-item">信號績效統計</a>
    <a href="backtest.html" class="nav-item">📊 回測中心</a>    <!-- 新增 -->
    <a href="settings.html" class="nav-item">⚙️ 後台設定</a>
</nav>
```

## 附錄 B：CSS 變數參考（部分）

```css
:root {  /* kawaii 主題 */
    --primary: #a78bfa;
    --buy-color: #34d399;    /* 綠色，正報酬 */
    --sell-color: #fb7185;   /* 紅色，負報酬 */
    --bg-dark: #fdf2f8;
    --card-bg: rgba(255,255,255,0.7);
    --text-primary: #4a3560;
    --border-color: rgba(167,139,250,0.15);
}
```

## 附錄 C：台股交易成本參考

| 項目 | 費率 | 說明 |
|------|------|------|
| 券商手續費 | 0.1425% | 買賣皆收，多數券商打 6 折 → 實際約 0.06% |
| 證交稅 | 0.3% | 僅賣出時收（ETF 為 0.1%） |
| 滑價 | 0~0.1% | 視流動性而定，回測預設 0 |

建議回測預設用 **0.1425%（未折扣）** 手續費，讓結果偏保守。使用者可自行調整。
