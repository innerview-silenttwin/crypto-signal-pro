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
from typing import Dict, Any, List
from datetime import datetime
import pandas as pd
import ccxt.async_support as ccxt_async
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi.responses import RedirectResponse
import io
import urllib.request
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from backend.data.twse_fetcher import TWSEFetcher

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
aggregator = SignalAggregator()

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
                        signal = aggregator.analyze(df, symbol=symbol, timeframe=tf)
                        
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
            import json
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


def fetch_yahoo_ohlcv(symbol: str, timeframe: str = "1d", limit: int = 200):
    """使用 Yahoo Finance 下載台股歷史 K 線資料（僅用於補充）。"""
    if '.' not in symbol:
        symbol = f"{symbol}.TW"

    interval_map = {
        '1d': '1d',
        '4h': '1h',
        '1h': '1h',
    }
    interval = interval_map.get(timeframe, '1d')

    end = int(datetime.now().timestamp())
    start = int((datetime.now() - timedelta(days=365)).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v7/finance/download/{symbol}"
        f"?period1={start}&period2={end}&interval={interval}&events=history&includeAdjustedClose=true"
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
        print(f"Yahoo fetch error ({symbol}): {e}")
        return None


# 台股期貨 Yahoo Finance 代碼對照表（主要嘗試 → 備用加權指數）
# TAIFEX 自有 API 皆需 JS 動態載入，目前以加權指數 ^TWII 作為技術分析代理
FUTURES_YAHOO_MAP = {
    'TX':  ['^TWII'],   # 台指期（以加權指數代理，技術指標完全相同）
    'MTX': ['^TWII'],   # 小台指（同上）
    'TE':  ['^TWII'],   # 電子期（以加權指數近似代理）
    'TF':  ['^TWII'],   # 金融期（以加權指數近似代理）
}

FUTURES_NAMES = {
    'TX':  '台指期',
    'MTX': '小台指',
    'TE':  '電子期',
    'TF':  '金融期',
}


def _fetch_yahoo_v8(yahoo_sym: str, interval: str, start: int, end: int):
    """Yahoo Finance v8 JSON API 內部抓取，成功回傳 DataFrame，失敗回傳 None。"""
    import ssl, json
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
        f"?period1={start}&period2={end}&interval={interval}"
    )
    context = ssl._create_unverified_context()
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    raw = urllib.request.urlopen(req, timeout=15, context=context).read().decode('utf-8')
    payload = json.loads(raw)
    result = payload.get('chart', {}).get('result', [])
    if not result:
        return None
    r = result[0]
    timestamps = r.get('timestamp', [])
    quote   = r.get('indicators', {}).get('quote', [{}])[0]
    opens   = quote.get('open',   [])
    highs   = quote.get('high',   [])
    lows    = quote.get('low',    [])
    closes  = quote.get('close',  [])
    volumes = quote.get('volume', [])

    records = []
    for i, ts in enumerate(timestamps):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        records.append({
            'timestamp': pd.Timestamp(ts, unit='s'),
            'open':   opens[i]   if i < len(opens)   and opens[i]   is not None else c,
            'high':   highs[i]   if i < len(highs)   and highs[i]   is not None else c,
            'low':    lows[i]    if i < len(lows)    and lows[i]    is not None else c,
            'close':  c,
            'volume': volumes[i] if i < len(volumes) and volumes[i] is not None else 0,
        })
    if not records:
        return None
    df = pd.DataFrame(records)
    df.set_index('timestamp', inplace=True)
    return df


def fetch_futures_ohlcv(symbol: str, timeframe: str = "1d", limit: int = 200):
    """抓取台股期貨歷史 K 線，依序嘗試 FUTURES_YAHOO_MAP 中各代碼。"""
    sym_key = symbol.upper().split('.')[0]
    candidates = FUTURES_YAHOO_MAP.get(sym_key, ['^TWII'])

    interval_map = {'1d': '1d', '4h': '1h', '1h': '1h'}
    interval = interval_map.get(timeframe, '1d')
    days = 730 if timeframe == '1d' else 60

    end   = int(datetime.now().timestamp())
    start = int((datetime.now() - timedelta(days=days)).timestamp())

    for yahoo_sym in candidates:
        try:
            df = _fetch_yahoo_v8(yahoo_sym, interval, start, end)
            if df is not None and len(df) > 0:
                print(f"Futures [{symbol}] fetched via {yahoo_sym}, rows={len(df)}")
                if len(df) > limit:
                    df = df.tail(limit)
                return df
        except Exception as e:
            print(f"Futures try {yahoo_sym} failed: {e}")

    print(f"Futures fetch: all candidates failed for {symbol}")
    return None


@app.get("/api/futures-info")
async def get_futures_info(symbol: str):
    """回傳台股期貨名稱對照。"""
    sym_key = symbol.upper().split('.')[0]
    name = FUTURES_NAMES.get(sym_key, '')
    return {"symbol": symbol, "name": name}


def fetch_stock_name(symbol: str):
    """查詢台股公司名稱（如：台積電）。"""
    if '.' not in symbol:
        symbol = f"{symbol}.TW"
    url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=price"
    try:
        import ssl, json
        context = ssl._create_unverified_context()
        raw = urllib.request.urlopen(url, timeout=15, context=context).read().decode('utf-8')
        payload = json.loads(raw)
        name = payload.get('quoteSummary', {}).get('result', [{}])[0].get('price', {}).get('longName')
        return name
    except Exception as e:
        print(f"Stock name fetch error ({symbol}): {e}")
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
    """盤後計算台股 / 期貨技術信號（使用日線 8 指標引擎）。"""
    if market == 'futures':
        df = fetch_futures_ohlcv(symbol, '1d', limit=200)
    else:
        # 信號端點優先使用 Yahoo v8（快速），TWSE 月份逐抓太慢
        yahoo_sym = symbol if '.' in symbol else f"{symbol}.TW"
        end   = int(datetime.now().timestamp())
        start = int((datetime.now() - timedelta(days=730)).timestamp())
        try:
            df = _fetch_yahoo_v8(yahoo_sym, '1d', start, end)
        except Exception:
            df = None
        if df is None:
            df = fetch_yahoo_ohlcv(symbol, '1d', limit=200)
        if df is None:
            df = fetch_twse_daily(symbol, limit=200, months=12)

    if df is None or len(df) < 30:
        return {"symbol": symbol, "signals": {}}

    signal = aggregator.analyze(df, symbol=symbol, timeframe='1d')
    return {
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
        }
    }


@app.get("/api/stock-info")
async def get_stock_info(symbol: str):
    """提供簡易股票名稱查詢，用於前端顯示。"""
    name = fetch_stock_name(symbol)
    return {"symbol": symbol, "name": name or ""}


@app.get("/api/chart")
async def get_chart_data(symbol: str = "BTC/USDT", timeframe: str = "1d", market: str = "crypto"):
    if market == 'futures':
        df = fetch_futures_ohlcv(symbol, timeframe, limit=200)
        if df is None:
            return []
        data = []
        for idx, row in df.iterrows():
            data.append({
                "time":   int(idx.timestamp()),
                "open":   float(row['open']),
                "high":   float(row['high']),
                "low":    float(row['low']),
                "close":  float(row['close']),
                "volume": float(row.get('volume', 0) or 0)
            })
        return data

    if market == 'stock':
        # 優先嘗試台灣證交所官方日線資料 (穩定且真實)
        df = fetch_twse_daily(symbol, limit=200, months=24)

        # 若 TWSE 失敗，再退回 Yahoo / Stooq
        if df is None:
            df = fetch_yahoo_ohlcv(symbol, timeframe, limit=200)

        if df is None:
            start_date = datetime.now() - timedelta(days=365)
            end_date = datetime.now()
            df = fetch_stooq_ohlcv(symbol, start_date, end_date, limit=200)
            if df is not None:
                print(f"Fetched stock data from Stooq for {symbol}")

        if df is not None:
            print(f"Fetched stock data for {symbol}, rows={len(df)}")

        # 若仍失敗，退回到本地示例資料（用 BTC 代替）
        if df is None:
            try:
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                local_csv = os.path.join(project_root, 'data', 'btc_daily_7y.csv')
                df = pd.read_csv(local_csv, index_col='timestamp', parse_dates=True).tail(200)
                print(f"Stock data fetch failed; using local BTC sample data for {symbol} (file={local_csv})")
            except Exception as e:
                print(f"Fallback local data load failed: {e}")
                df = None

        if df is not None:
            data = []
            for idx, row in df.iterrows():
                data.append({
                    "time": int(idx.timestamp()),
                    "open": float(row['open']),
                    "high": float(row['high']),
                    "low": float(row['low']),
                    "close": float(row['close']),
                    "volume": float(row.get('volume', 0) or 0)
                })
            return data
        return []

    exchange = ccxt_async.binance({'enableRateLimit': True})
    try:
        df = await fetch_ohlcv_async(exchange, symbol, timeframe, limit=200)
        await exchange.close()
        if df is not None:
            data = []
            for idx, row in df.iterrows():
                data.append({
                    "time": int(idx.timestamp()),
                    "open": float(row['open']),
                    "high": float(row['high']),
                    "low": float(row['low']),
                    "close": float(row['close']),
                    "volume": float(row['volume'])
                })
            return data
        return []
    except Exception as e:
        print(f"Chart fetch error: {e}")
        await exchange.close()
        return []

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
        import json
        if current_signals:
            await websocket.send_text(json.dumps({"type": "init", "data": list(current_signals.values())}))
        
        while True:
            # 保持連線
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    # 直接啟動 Server
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
