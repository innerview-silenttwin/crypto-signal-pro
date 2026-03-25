"""
FastAPI 主程式 - 即時信號伺服器 (Phase 2)

負責：
1. 提供 REST API 獲取最新信號
2. 透過 WebSocket 推送即時價格與信號更新
3. 提供靜態網頁 (Frontend Dashboard) 的伺服
"""

import sys
import os
import asyncio
import json
from typing import List
from datetime import datetime
import time
import pandas as pd
import yfinance as yf
import ccxt.async_support as ccxt_async
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
import io
import urllib.request
import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator, MarketType
from trading_manager import trading_manager

app = FastAPI(title="CryptoSignal Pro API", version="1.0.0")

# 允許跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 確保這能抓到正確的 frontend 資料夾
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
frontend_path = os.path.join(project_root, "frontend")
app.mount("/dashboard", StaticFiles(directory=frontend_path, html=True), name="frontend")
@app.get("/")
async def redirect_to_dashboard():
    return RedirectResponse(url="/dashboard/")

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()

# 不同市場各自的聚合器（權重策略不同）
aggregator_crypto = SignalAggregator(MarketType.CRYPTO)
aggregator_stock = SignalAggregator(MarketType.STOCK)
aggregator_futures = SignalAggregator(MarketType.FUTURES)

def get_aggregator(market: str = "crypto") -> SignalAggregator:
    """根據市場類型取得對應聚合器"""
    if market == "stock":
        return aggregator_stock
    elif market == "futures":
        return aggregator_futures
    return aggregator_crypto

# 全域狀態
current_signals = {}
symbols_to_track = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
timeframes = ["1d", "4h", "1h"]

async def fetch_ohlcv_async(exchange, symbol, timeframe, limit=200):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df
    except Exception as e:
        print(f"Error fetching {symbol} {timeframe}: {e}")
        return None

async def background_signal_updater():
    """背景任務：定期抓取資料並更新信號"""
    exchange = ccxt_async.binance({'enableRateLimit': True})
    
    while True:
        try:
            timestamp_now = datetime.now().strftime("%H:%M:%S")
            updates = []
            
            for symbol in symbols_to_track:
                symbol_data = {"symbol": symbol, "signals": {}}
                
                # 同時抓取多個時間框架
                tasks = [fetch_ohlcv_async(exchange, symbol, tf) for tf in timeframes]
                results = await asyncio.gather(*tasks)
                
                for tf, df in zip(timeframes, results):
                    if df is not None and len(df) > 0:
                        # 分析信號
                        signal = aggregator_crypto.analyze(df, symbol=symbol, timeframe=tf)
                        
                        signal_data = {
                            "timeframe": tf,
                            "price": round(signal.price, 2),
                            "direction": signal.direction,
                            "confidence": round(signal.confidence, 1),
                            "level": signal.signal_level,
                            "buy_score": round(signal.buy_score, 1),
                            "sell_score": round(signal.sell_score, 1),
                            "timestamp": timestamp_now,
                            "last_candle": {
                                "open": float(df['open'].iloc[-1]),
                                "high": float(df['high'].iloc[-1]),
                                "low": float(df['low'].iloc[-1]),
                                "close": float(df['close'].iloc[-1]),
                            }
                        }
                        symbol_data["signals"][tf] = signal_data
                
                current_signals[symbol] = symbol_data
                updates.append(symbol_data)
            
            # 廣播給所有前端客戶端
            await manager.broadcast(json.dumps({"type": "update", "data": updates}))
            
        except Exception as e:
            print(f"背景任務錯誤: {e}")
            
        # 等待 30 秒後再次更新（展示用可調低以增加即時感）
        await asyncio.sleep(10)
        
    await exchange.close()

@app.on_event("startup")
async def startup_event():
    # 啟動背景更新任務
    asyncio.create_task(background_signal_updater())

def fetch_stooq_ohlcv(symbol: str, start_date: datetime, end_date: datetime, limit: int = 200):
    """使用 Stooq 下載台股日線歷史資料（較少被封鎖）。"""
    if '.' not in symbol:
        symbol = f"{symbol}.TW"
    stooq_code = symbol.replace('.', '').lower()  # e.g. 2330.tw -> 2330tw

    url = (
        f"https://stooq.com/q/d/l/?s={stooq_code}"
        f"&d1={start_date.strftime('%Y%m%d')}"
        f"&d2={end_date.strftime('%Y%m%d')}"
        f"&i=d"
    )

    try:
        import ssl
        context = ssl._create_unverified_context()
        raw = urllib.request.urlopen(url, timeout=15, context=context).read().decode('utf-8')
        df = pd.read_csv(io.StringIO(raw), parse_dates=['Date'])
        df.rename(columns={
            'Date': 'timestamp',
            'Open': 'open',
            'High': 'high',
            'Low': 'low',
            'Close': 'close',
            'Volume': 'volume',
        }, inplace=True)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df.set_index('timestamp', inplace=True)
        if len(df) > limit:
            df = df.tail(limit)
        return df
    except Exception as e:
        print(f"Stooq fetch error ({symbol}): {e}")
        return None


# FUTURES_NAMES 用於期貨代碼對照
FUTURES_NAMES = {
    'TX':  '台指期',
    'MTX': '小台指',
    'TE':  '電子期',
    'TF':  '金融期',
}

def fetch_futures_ohlcv(symbol: str, timeframe: str = "1d", limit: int = 200):
    """期貨歷史資料暫時停用，待後續串接其他資料源"""
    return None


@app.get("/api/futures-info")
async def get_futures_info(symbol: str):
    """回傳台股期貨名稱對照。"""
    sym_key = symbol.upper().split('.')[0]
    name = FUTURES_NAMES.get(sym_key, '')
    return {"symbol": symbol, "name": name}


def fetch_stock_name(symbol: str):
    """查詢台股公司名稱（如：台積電），並帶有常用股票快取。"""
    raw_symbol = symbol.split('.')[0] if '.' in symbol else symbol
    
    # 內建台股各類股市值前十大公司對照表
    common_stocks = {
        # 半導體
        '2330': '台積電', '2454': '聯發科', '2303': '聯電', '3711': '日月光投控', '2379': '瑞昱',
        '2337': '旺宏', '2344': '華邦電', '2408': '南亞科', '3443': '創意', '3661': '世芯-KY',
        # 電子代工/零組件/光電
        '2317': '鴻海', '2382': '廣達', '3231': '緯創', '2308': '台達電', '2357': '華碩',
        '2324': '仁寶', '2353': '宏碁', '3008': '大立光', '2395': '研華', '2376': '技嘉',
        # 金融
        '2881': '富邦金', '2882': '國泰金', '2891': '中信金', '2886': '兆豐金', '2884': '玉山金',
        '2892': '第一金', '2885': '元大金', '2880': '華南金', '2883': '開發金', '2887': '台新金',
        # 傳產/航運/電信等
        '1301': '台塑', '1303': '南亞', '1326': '台化', '6505': '台塑化', '2002': '中鋼',
        '1101': '台泥', '1102': '亞泥', '1216': '統一', '2207': '和泰車', '2412': '中華電',
        '3045': '台灣大', '4904': '遠傳', '2603': '長榮', '2609': '陽明', '2615': '萬海'
    }
    
    if raw_symbol in common_stocks:
        return common_stocks[raw_symbol]
        
    # 嘗試官方 TWSE API (免授權、無反爬蟲)
    try:
        import urllib.request, json, ssl
        context = ssl._create_unverified_context()
        url = f"https://www.twse.com.tw/zh/api/codeQuery?query={raw_symbol}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        raw = urllib.request.urlopen(req, timeout=10, context=context).read().decode('utf-8')
        payload = json.loads(raw)
        
        suggestions = payload.get('suggestions', [])
        for s in suggestions:
            # TWSE API 會回傳例如 "3008\t大立光"
            parts = s.split('\t')
            if len(parts) == 2 and parts[0] == raw_symbol:
                return parts[1]
    except Exception as e:
        print(f"TWSE name fetch error ({raw_symbol}): {e}")

    # 都找不到的話回傳原始代碼名稱
    return None


def fetch_twse_daily(symbol: str, limit: int = 200, months: int = 12):
    """從台灣證交所官方 API 下載每日收盤資料。

    目前會從當月往回抓指定月數，並回傳最近 `limit` 筆資料。
    此 API 不需授權，適合拿來做歷史日線。（但不適合高頻或分鐘級）
    """
    if '.' in symbol:
        symbol = symbol.split('.')[0]

    def month_iter(year, month, count):
        for _ in range(count):
            yield year, month
            month -= 1
            if month == 0:
                month = 12
                year -= 1

    collected = []
    now = datetime.now()
    for year, month in month_iter(now.year, now.month, months):
        date_param = f"{year}{month:02d}01"
        url = (
            f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=csv"
            f"&date={date_param}&stockNo={symbol}"
        )
        try:
            import ssl
            context = ssl._create_unverified_context()
            raw_bytes = urllib.request.urlopen(url, timeout=15, context=context).read()
            raw = raw_bytes.decode('big5', errors='ignore')

            # TWSE 會回傳一些說明文字，真正的 CSV 以「日期,成交股數,...」開頭
            lines = [l for l in raw.splitlines() if l.strip()]
            idx = next((i for i, l in enumerate(lines) if '日期' in l and '成交股數' in l), None)
            if idx is None:
                continue
            csv_text = '\n'.join(lines[idx:])
            df = pd.read_csv(io.StringIO(csv_text))

            # 清理資料
            df = df.rename(columns={
                '日期': 'date',
                '開盤價': 'open',
                '最高價': 'high',
                '最低價': 'low',
                '收盤價': 'close',
                '成交股數': 'volume',
            })
            df = df[['date', 'open', 'high', 'low', 'close', 'volume']]

            def parse_twse_date(v):
                try:
                    parts = v.split('/')
                    if len(parts) == 3:
                        y = int(parts[0]) + 1911
                        m = int(parts[1])
                        d = int(parts[2])
                        return pd.Timestamp(year=y, month=m, day=d)
                except Exception:
                    pass
                return pd.NaT

            df['date'] = df['date'].astype(str).apply(parse_twse_date)

            # 可能有 '--' 表示漲停跌停，可轉成 NaN
            df = df.replace({'--': None})
            df = df.dropna(subset=['date', 'open', 'high', 'low', 'close'])
            # 去掉千分位逗號
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(str).str.replace(',', '').astype(float)

            collected.append(df)
            # 如果資料量夠了就跳
            if sum(len(x) for x in collected) >= limit:
                break
        except Exception as e:
            print(f"TWSE fetch error ({symbol} {date_param}): {e}")
            continue

    if not collected:
        return None

    df_all = pd.concat(collected, ignore_index=True)
    df_all = df_all.sort_values(by='date')
    if len(df_all) > limit:
        df_all = df_all.tail(limit)

    df_all.set_index('date', inplace=True)
    return df_all


@app.get("/api/tw-signals")
async def get_tw_signals(symbol: str, market: str = "stock"):
    """盤後計算台股 / 期貨技術信號（使用日線 8 指標引擎），帶 60 秒快取。"""
    import math
    cache_key = f"signals_{symbol}"
    now = time.time()
    remaining = tw_seconds_until_next()

    # 若快取存在且仍在 rate limit 視窗內，直接回傳快取
    if cache_key in signals_cache:
        cached = signals_cache[cache_key]
        age = now - cached["fetched_at"]
        if age < TW_RATE_LIMIT_SEC:
            print(f"[signals cache] {symbol} (age={int(age)}s)")
            result = dict(cached["data"])
            result["next_update_in"] = remaining
            result["data_source"] = "signals_cache"
            return result

    if market == 'futures':
        df = None  # 暫無可用的期貨資料源
    else:
        df = fetch_twse_daily(symbol, limit=200, months=12)

    if df is None or len(df) < 30:
        return {"symbol": symbol, "signals": {}, "next_update_in": remaining, "data_source": "twse_daily"}

    agg = get_aggregator(market)
    signal = agg.analyze(df, symbol=symbol, timeframe='1d')
    result_data = {
        "symbol": symbol,
        "signals": {
            "1d": {
                "timeframe": "1d",
                "price":      round(signal.price, 2),
                "direction":  signal.direction,
                "confidence": round(signal.confidence, 1),
                "level":      signal.signal_level,
                "buy_score":  round(signal.buy_score, 1),
                "sell_score": round(signal.sell_score, 1),
            }
        },
        "data_source": "twse_daily",
        "next_update_in": TW_RATE_LIMIT_SEC
    }
    # 寫入 signals cache
    signals_cache[cache_key] = {
        "data": result_data,
        "fetched_at": now
    }
    return result_data


@app.get("/api/stock-info")
async def get_stock_info(symbol: str):
    """提供簡易股票名稱查詢，用於前端顯示。"""
    name = fetch_stock_name(symbol)
    return {"symbol": symbol, "name": name or ""}


# ============================================================
# 台股 / 台指期 Rate Limiter + Cache
# 規則：整個「台灣市場」共用一個 60 秒的請求視窗。
# 視窗內不管哪支股票、哪個時間框架，一律回傳快取資料。
# 視窗到期後，下一次請求會真正向 yfinance 發出網路連線。
# ============================================================
TW_RATE_LIMIT_SEC = 60

# 全域快取字典
# chart_cache: {"symbol_timeframe": {"candles": [...], "fetched_at": float, "source": str}}
chart_cache: dict = {}
# signals_cache: {"symbol": {"data": {...}, "fetched_at": float}}
signals_cache: dict = {}
# rate_limiter: 記錄「最後一次真實向 yfinance 發出請求」的時間戳
# 這是整個台灣市場共用的，不區分個股
tw_last_real_fetch: float = 0.0

def tw_can_fetch_now() -> bool:
    """判斷距上一次真實請求是否已超過 60 秒。"""
    return (time.time() - tw_last_real_fetch) >= TW_RATE_LIMIT_SEC

def tw_seconds_until_next() -> int:
    """距下一次可請求還有幾秒。"""
    elapsed = time.time() - tw_last_real_fetch
    remaining = max(0, TW_RATE_LIMIT_SEC - elapsed)
    return int(remaining)


def is_tw_market_open() -> bool:
    """判斷台灣市場目前是否在交易時段 (週一至五 09:00 - 14:00)。"""
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    # 週六(5), 週日(6) 不開市
    if now.weekday() >= 5:
        return False
    # 09:00 - 14:00 (含緩衝至 14:00)
    current_time = now.time()
    return (current_time >= datetime.strptime("09:00", "%H:%M").time() and 
            current_time <= datetime.strptime("14:15", "%H:%M").time())

@app.get("/api/ping")
async def ping():
    return {"status": "ok", "server_time": time.time()}

def fetch_yfinance_candles(symbol: str, timeframe: str, limit: int = 200):
    """不帶快取、直接向 yfinance 抓資料，回傳 (candles_list, source_str)。"""
    global tw_last_real_fetch

    yf_symbol = f"{symbol}.TW" if '.' not in symbol else symbol
    print(f"[yfinance] Fetching {yf_symbol} ({timeframe})")

    yf_interval = "1d"
    period = "1y"
    if timeframe in ["1m", "5m", "15m", "30m", "60m", "1h", "4h"]:
        yf_interval = "60m" if timeframe in ["1h", "4h"] else timeframe
        period = "1mo"

    try:
        ticker = yf.Ticker(yf_symbol)
        df = ticker.history(period=period, interval=yf_interval)
        if df.empty:
            return None, None

        if timeframe == "4h":
            df = df.resample('4h').agg({
                'Open': 'first', 'High': 'max',
                'Low': 'min', 'Close': 'last', 'Volume': 'sum'
            }).dropna()

        candles = []
        for idx, row in df.iterrows():
            candles.append({
                "time": int(idx.timestamp()),
                "open": float(row['Open']),
                "high": float(row['High']),
                "low":  float(row['Low']),
                "close": float(row['Close']),
                "volume": float(row.get('Volume', 0) or 0)
            })
        if len(candles) > limit:
            candles = candles[-limit:]

        # 記錄本次真實請求時間
        tw_last_real_fetch = time.time()
        return candles, "yfinance"
    except Exception as e:
        print(f"[yfinance] Error for {symbol}: {e}")
        return None, None


def get_tw_chart_data(symbol: str, timeframe: str, limit: int = 200):
    """
    台股走勢圖資料取得（帶嚴格全域 Rate Limit + Cache）。
    """
    cache_key = f"{symbol}_{timeframe}"
    now = time.time()
    cached = chart_cache.get(cache_key)
    remaining = tw_seconds_until_next()
    market_open = is_tw_market_open()

    # ============================================================
    # 路徑 A：60 秒已過，且在交易時段，允許真實請求
    # ============================================================
    if tw_can_fetch_now() and market_open:
        candles, source = fetch_yfinance_candles(symbol, timeframe, limit)
        if candles:
            chart_cache[cache_key] = {"candles": candles, "fetched_at": tw_last_real_fetch, "source": "yfinance"}
            return {"candles": candles, "data_source": "yfinance", "fetched_at": tw_last_real_fetch, "next_update_in": TW_RATE_LIMIT_SEC}

        # yfinance 失敗 → TWSE 每日歷史備案（僅日線）
        if timeframe == "1d":
            df = fetch_twse_daily(symbol, limit=limit, months=24)
            if df is not None:
                candles = [{"time": int(idx.timestamp()), "open": float(row['open']), "high": float(row['high']),
                             "low": float(row['low']), "close": float(row['close']), "volume": float(row.get('volume', 0) or 0)}
                            for idx, row in df.iterrows()]
                chart_cache[cache_key] = {"candles": candles, "fetched_at": now, "source": "twse_daily"}
                return {"candles": candles, "data_source": "twse_daily", "fetched_at": now, "next_update_in": TW_RATE_LIMIT_SEC}

        # 有過期快取則回傳，避免空白
        if cached:
            return {"candles": cached["candles"], "data_source": cached["source"] + "_cache",
                    "fetched_at": cached["fetched_at"], "next_update_in": TW_RATE_LIMIT_SEC}
        return None

    # ============================================================
    # 路徑 B：60 秒未到 OR 盤後時段，禁止頻繁向 yfinance 請求
    # ============================================================
    print(f"[rate-limit] Blocked/Closed. TradeOpen={market_open}, Left={remaining}s")

    # B0: 盤後時段特別處理資料來源文字
    src_suffix = "" if market_open else "_closed"

    # B1: 有快取 → 直接回傳
    if cached:
        return {"candles": cached["candles"], "data_source": cached["source"] + "_cache" + src_suffix,
                "fetched_at": cached["fetched_at"], "next_update_in": remaining}

    # B2: 沒有快取 + 日線 → TWSE 備案
    if timeframe == "1d":
        df = fetch_twse_daily(symbol, limit=limit, months=24)
        if df is not None:
            candles = [{"time": int(idx.timestamp()), "open": float(row['open']), "high": float(row['high']),
                         "low": float(row['low']), "close": float(row['close']), "volume": float(row.get('volume', 0) or 0)}
                        for idx, row in df.iterrows()]
            return {"candles": candles, "data_source": "twse_daily" + src_suffix, "fetched_at": now, "next_update_in": remaining}

    # B3: 盤後時段且非日線且無快取，最後嘗試一次 yfinance (僅此一次載入)
    if not market_open and not cached:
         candles, source = fetch_yfinance_candles(symbol, timeframe, limit)
         if candles:
            chart_cache[cache_key] = {"candles": candles, "fetched_at": now, "source": "yfinance"}
            return {"candles": candles, "data_source": "yfinance_closed", "fetched_at": now, "next_update_in": 3600}

    # B3: 沒有快取 + 非日線 + 限流中 → 回傳空，前端顯示倒數等待
    print(f"[rate-limit] No cache/fallback for {cache_key}, returning rate_limited ({remaining}s)")
    return {"candles": [], "data_source": "rate_limited", "fetched_at": now, "next_update_in": remaining}

@app.get("/api/chart")
async def get_chart_data(symbol: str = "BTC/USDT", timeframe: str = "1d", market: str = "crypto"):
    if market == 'futures':
        # 期貨也使用相同的 rate limiter 機制（目前無資料源，保留架構）
        result = get_tw_chart_data(symbol, timeframe, limit=200)
        if result and result["candles"]:
            return {
                "candles": result["candles"],
                "data_source": result["data_source"],
                "next_update_in": result["next_update_in"]
            }
        return {"candles": [], "data_source": None, "next_update_in": 0}

    if market == 'stock':
        result = get_tw_chart_data(symbol, timeframe, limit=200)
        if result and result["candles"]:
            return {
                "candles": result["candles"],
                "data_source": result["data_source"],
                "next_update_in": result["next_update_in"]
            }
        return {"candles": [], "data_source": None, "next_update_in": 0}

    exchange = ccxt_async.binance({'enableRateLimit': True})
    try:
        df = await fetch_ohlcv_async(exchange, symbol, timeframe, limit=200)
        await exchange.close()
        if df is not None:
            candles = []
            for idx, row in df.iterrows():
                candles.append({
                    "time": int(idx.timestamp()),
                    "open": float(row['open']),
                    "high": float(row['high']),
                    "low": float(row['low']),
                    "close": float(row['close']),
                    "volume": float(row['volume'])
                })
            return {"candles": candles, "data_source": "ccxt", "next_update_in": None}
        return {"candles": [], "data_source": None, "next_update_in": None}
    except Exception as e:
        print(f"Chart fetch error: {e}")
        await exchange.close()
        return {"candles": [], "data_source": None, "next_update_in": None}

@app.get("/api/signals")
async def get_signals():
    """獲取最新信號"""
    return current_signals

@app.websocket("/ws/signals")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 終端，推送即時信號"""
    await manager.connect(websocket)
    try:
        # 連線成功先推送一次目前狀態
        if current_signals:
            await websocket.send_text(json.dumps({"type": "init", "data": list(current_signals.values())}))
        
        while True:
            # 保持連線
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ============================================================
# 虛擬交易 API（供 trading.html 使用）
# ============================================================

@app.post("/api/trading/toggle")
async def toggle_trading(active: bool = False):
    """啟動/停止自動交易"""
    is_active = trading_manager.toggle_active(active)
    return {"is_active": is_active}

@app.get("/api/trading/status")
async def get_trading_status():
    """取得帳戶摘要（資產淨值、持倉、損益）"""
    # 嘗試取得最新價格用於計算未實現損益
    current_prices = {}
    for symbol, data in current_signals.items():
        sigs = data.get("signals", {})
        if "1d" in sigs:
            current_prices[symbol] = sigs["1d"].get("price", 0)
    return trading_manager.get_summary(current_prices)

@app.get("/api/trading/history")
async def get_trading_history(page: int = 1, pageSize: int = 15,
                               symbol: str = "", startDate: str = "", endDate: str = ""):
    """取得交易歷史（支援篩選與分頁）"""
    history = trading_manager.state.get("history", [])

    # 篩選
    if symbol:
        history = [h for h in history if symbol.upper() in h.get("symbol", "").upper()]
    if startDate:
        history = [h for h in history if h.get("time", "") >= startDate]
    if endDate:
        history = [h for h in history if h.get("time", "")[:10] <= endDate]

    total = len(history)
    start = (page - 1) * pageSize
    end = start + pageSize
    return {"data": history[start:end], "total": total, "page": page}

@app.get("/api/trading/symbols")
async def get_watchlist_symbols():
    """取得監控標的清單"""
    return trading_manager.state.get("symbols", [])

@app.post("/api/trading/symbols/add")
async def add_watchlist_symbol(symbol: str):
    """新增監控標的"""
    success = trading_manager.add_symbol(symbol)
    return {"success": success, "symbols": trading_manager.state.get("symbols", [])}

@app.post("/api/trading/symbols/remove")
async def remove_watchlist_symbol(symbol: str):
    """移除監控標的"""
    success = trading_manager.remove_symbol(symbol)
    return {"success": success, "symbols": trading_manager.state.get("symbols", [])}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
