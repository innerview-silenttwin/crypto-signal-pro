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
import logging
import re
from typing import List, Dict, Tuple
from datetime import datetime, timedelta
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from signals.aggregator import SignalAggregator, MarketType
from business.sentiment import sentiment_engine
from trading_manager import trading_manager

# ============================================================
# 路徑常量
# ============================================================
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(BACKEND_DIR, "data", "history", "stock")
TW_RATE_STATE_PATH = os.path.join(BACKEND_DIR, "data", "tw_rate_state.json")

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

                        # 計算 24h 漲跌幅
                        change_24h = 0.0
                        if len(df) >= 2:
                            prev_close = float(df['close'].iloc[-2])
                            curr_close = float(df['close'].iloc[-1])
                            if prev_close > 0:
                                change_24h = round((curr_close - prev_close) / prev_close * 100, 2)

                        signal_data = {
                            "timeframe": tf,
                            "price": round(signal.price, 2),
                            "direction": signal.direction,
                            "confidence": round(signal.confidence, 1),
                            "level": signal.signal_level,
                            "buy_score": round(signal.buy_score, 1),
                            "sell_score": round(signal.sell_score, 1),
                            "change_24h": change_24h,
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
            
            # 記錄 crypto 更新時間
            last_update_timestamps["crypto"] = datetime.now().strftime("%H:%M:%S")
            # 情緒引擎：取得最新事件與倒數
            sentiment_data = sentiment_engine.get_latest_sentiment()
            # 廣播給所有前端客戶端
            broadcast_payload = {"type": "update", "data": updates}
            if sentiment_data:
                broadcast_payload["global_alert"] = sentiment_data
            await manager.broadcast(json.dumps(broadcast_payload))
            
        except Exception as e:
            print(f"背景任務錯誤: {e}")
            
        # 等待 30 秒後再次更新（展示用可調低以增加即時感）
        await asyncio.sleep(10)
        
    await exchange.close()

@app.on_event("startup")
async def startup_event():
    # 啟動背景更新任務
    asyncio.create_task(background_signal_updater())
    # 預載台股 ticker 資料（L2 本地 CSV → L3 TWSE，尊重 rate limit）
    asyncio.create_task(preload_tw_ticker_data())
    # 每日 14:35 自動刷新台股 ticker 資料（避免資料停滯）
    asyncio.create_task(daily_tw_data_refresh())
    # 主動 ETF 持股分數獨立刷新（每 4 小時，避免被 daily-refresh 拖累或漏跑）
    asyncio.create_task(active_etf_refresh_worker())
    # 背景消化過期 CSV 刷新佇列（用戶訪問時觸發）
    asyncio.create_task(stale_refresh_worker())
    # 自動啟動類股交易引擎
    try:
        from sector_auto_trader import auto_trader as _sat
        _sat.start()
        print("[Startup] 類股自動交易引擎已啟動")
    except Exception as e:
        print(f"[Startup] 類股交易引擎啟動失敗: {e}")
    # 自動啟動 BTC 交易引擎
    try:
        from btc_auto_trader import btc_trader as _bt
        _bt.start()
        print("[Startup] BTC 自動交易引擎已啟動")
    except Exception as e:
        print(f"[Startup] BTC 交易引擎啟動失敗: {e}")
    # 啟動每日績效報告排程（每晚 21:00）
    try:
        from daily_report import daily_report_scheduler
        asyncio.create_task(daily_report_scheduler())
        print("[Startup] 每日績效報告排程已啟動（21:00）")
    except Exception as e:
        print(f"[Startup] 每日績效報告排程啟動失敗: {e}")


async def preload_tw_ticker_data():
    """Server 啟動時自動預載所有台股 ticker 標的的資料。
    優先讀本地 CSV，若無本地資料且 rate limit 允許，才抓 TWSE。
    """
    await asyncio.sleep(2)  # 等 server 完全啟動
    for tw_sym, tw_market in TW_TICKER_SYMBOLS:
        cache_key = f"signals_{tw_sym}"
        # 已有 L1 快取就跳過
        if cache_key in signals_cache:
            continue

        # 嘗試 L2: 本地 CSV（資料超過 4 天則視為過期，改抓 TWSE）
        local_df = await asyncio.to_thread(load_local_history, tw_sym)
        if local_df is not None and len(local_df) >= 30:
            last_idx = local_df.index[-1]
            last_date = last_idx.date() if hasattr(last_idx, 'date') else pd.to_datetime(last_idx).date()
            days_old = (datetime.now().date() - last_date).days
            if days_old <= 4:
                print(f"[preload] {tw_sym} from local CSV ({len(local_df)} rows, {days_old}d old)")
                _analyze_tw_df(tw_sym, tw_market, local_df, "local_csv_preload")
                continue
            print(f"[preload] {tw_sym} local CSV stale ({days_old}d old), fetching fresh data...")

        # 嘗試 L3: TWSE API（尊重 rate limit；同步 IO 丟 thread pool）
        if tw_market != 'futures':
            df = await asyncio.to_thread(_fetch_tw_df, tw_sym, tw_market)
            if df is not None:
                print(f"[preload] {tw_sym} from TWSE API")
                _analyze_tw_df(tw_sym, tw_market, df, "twse_preload")
                continue

            # 最後手段：如果完全沒資料，強制抓一次（忽略 rate limit，僅啟動時）
            if cache_key not in signals_cache:
                print(f"[preload] {tw_sym} no data anywhere, one-time TWSE fetch...")
                df = await asyncio.to_thread(fetch_twse_daily, tw_sym, 200, 12)
                if df is not None and len(df) >= 30:
                    await asyncio.to_thread(save_local_history, tw_sym, df)
                    _analyze_tw_df(tw_sym, tw_market, df, "twse_preload_forced")
                    # 更新 rate limit 時間戳，避免後續重複抓
                    global tw_last_real_fetch
                    tw_last_real_fetch = time.time()
                    _save_tw_rate_state()

    print(f"[preload] TW ticker preload complete. Cache keys: {list(signals_cache.keys())}")


async def daily_tw_data_refresh():
    """每個交易日盤後（14:35）自動刷新所有 active 標的（5 ticker + 自選股 + 5 產業池）。"""
    while True:
        now = datetime.now()
        today_refresh = now.replace(hour=14, minute=35, second=0, microsecond=0)
        next_refresh = today_refresh if now < today_refresh else today_refresh + timedelta(days=1)
        wait_seconds = (next_refresh - datetime.now()).total_seconds()
        print(f"[daily-refresh] Next TW data refresh scheduled in {int(wait_seconds/3600)}h {int((wait_seconds%3600)/60)}m")
        await asyncio.sleep(wait_seconds)

        # 只在工作日（週一~週五）執行
        if datetime.now().weekday() < 5:
            universe = _collect_active_universe()
            ticker_set = {s for s, _ in TW_TICKER_SYMBOLS}
            print(f"[daily-refresh] Starting daily TW stock data refresh ({len(universe)} symbols)...")
            global tw_last_real_fetch
            for tw_sym, tw_market in universe:
                if tw_market == 'futures':
                    continue
                try:
                    # 同步 IO 全部丟 thread pool，避免阻塞 event loop
                    df = await asyncio.to_thread(fetch_twse_daily, tw_sym, 200, 12)
                    if df is None or len(df) < 30:
                        df = await asyncio.to_thread(_fetch_yfinance_df, tw_sym)
                    if df is not None and len(df) >= 30:
                        await asyncio.to_thread(save_local_history, tw_sym, df)
                        # 只有跑馬燈 ticker 需要進 signals_cache
                        if tw_sym in ticker_set:
                            _analyze_tw_df(tw_sym, tw_market, df, "twse_daily_refresh")
                        tw_last_real_fetch = time.time()
                        _save_tw_rate_state()
                        print(f"[daily-refresh] Updated {tw_sym}: {len(df)} rows")
                    else:
                        print(f"[daily-refresh] {tw_sym}: no data")
                    await asyncio.sleep(8)  # 每檔間隔 8 秒，避免 TWSE rate limit
                except Exception as e:
                    print(f"[daily-refresh] Error refreshing {tw_sym}: {e}")

            print("[daily-refresh] Daily TW stock data refresh complete.")
        else:
            print("[daily-refresh] Weekend, skipping TW data refresh.")


async def active_etf_refresh_worker():
    """獨立刷新主動 ETF 持股分數：啟動時若快取過期立即跑一次，之後每 4 小時跑一次。

    與 daily_tw_data_refresh 解耦避免被 symbol loop 拖累或漏跑。
    """
    from datetime import date as _date
    from layers.active_etf import refresh_active_etf_scores, _CACHE_FILE

    # 啟動時延遲 30 秒（讓其他啟動任務先就緒），檢查是否需要立即刷新
    await asyncio.sleep(30)

    while True:
        try:
            # 檢查快取新鮮度
            need_refresh = True
            if os.path.exists(_CACHE_FILE):
                try:
                    with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                        cache_data = json.load(f)
                    cache_date = _date.fromisoformat(cache_data.get("date", "2000-01-01"))
                    today = _date.today()
                    days_old = (today - cache_date).days
                    if days_old <= 0:
                        print(f"[active-etf-refresh] 快取為今日（{cache_date}），略過")
                        need_refresh = False
                    else:
                        print(f"[active-etf-refresh] 快取已過期 {days_old} 天（{cache_date}），刷新")
                except Exception as e:
                    print(f"[active-etf-refresh] 讀取快取失敗，強制刷新: {e}")

            if need_refresh:
                ok = await asyncio.to_thread(refresh_active_etf_scores)
                print(f"[active-etf-refresh] 結果: {'OK' if ok else 'FAILED'}")
        except Exception as e:
            print(f"[active-etf-refresh] worker 例外: {e}")

        # 每 4 小時檢查一次（盤中 + 盤後都會涵蓋）
        await asyncio.sleep(4 * 3600)


async def stale_refresh_worker():
    """背景刷新過期 CSV：每 10 秒掃 _stale_refresh_pending，rate limit 允許就抓一支。
    所有同步 IO 透過 to_thread 丟到 thread pool，避免阻塞 event loop。
    """
    while True:
        try:
            if _stale_refresh_pending and tw_can_fetch_now():
                sym = _stale_refresh_pending.pop()
                try:
                    df = await asyncio.to_thread(fetch_twse_daily, sym, 200, 12)
                    if df is None or len(df) < 30:
                        df = await asyncio.to_thread(_fetch_yfinance_df, sym)
                    if df is not None and len(df) >= 30:
                        await asyncio.to_thread(save_local_history, sym, df)
                        global tw_last_real_fetch
                        tw_last_real_fetch = time.time()
                        _save_tw_rate_state()
                        # 清掉前端 chart_cache，下次訪問重新從 CSV 讀
                        for k in list(chart_cache.keys()):
                            if k.startswith(f"{sym}_"):
                                chart_cache.pop(k, None)
                        print(f"[stale-refresh] Updated {sym}: {len(df)} rows")
                    else:
                        print(f"[stale-refresh] {sym}: no data from TWSE/yfinance")
                except Exception as e:
                    print(f"[stale-refresh] Error refreshing {sym}: {e}")
            await asyncio.sleep(10)
        except Exception as e:
            print(f"[stale-refresh] worker loop error: {e}")
            await asyncio.sleep(30)


def _maybe_queue_stale_refresh(symbol: str, local_df) -> int:
    """若 CSV 最後一筆 > 4 天舊則排隊背景刷新，回傳 days_old（無法判斷則回 0）。"""
    try:
        last_idx = local_df.index[-1]
        last_date = last_idx.date() if hasattr(last_idx, 'date') else pd.to_datetime(last_idx).date()
        days_old = (datetime.now().date() - last_date).days
        if days_old > 4 and symbol not in _stale_refresh_pending:
            _stale_refresh_pending.add(symbol)
            print(f"[stale-check] {symbol} CSV is {days_old}d old, queued for refresh")
        return days_old
    except Exception:
        return 0


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


def _analyze_tw_df(symbol: str, market: str, df, data_source: str):
    """從 DataFrame 計算信號並回傳標準結構（共用邏輯）。"""
    agg = get_aggregator(market)
    signal = agg.analyze(df, symbol=symbol, timeframe='1d')

    tw_change = 0.0
    if len(df) >= 2:
        prev_c = float(df['close'].iloc[-2])
        curr_c = float(df['close'].iloc[-1])
        if prev_c > 0:
            tw_change = round((curr_c - prev_c) / prev_c * 100, 2)

    market_open = is_tw_market_open()
    remaining = tw_seconds_until_next() if market_open else None

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
                "change_24h": tw_change,
            }
        },
        "data_source": data_source,
        "next_update_in": remaining,
        "market_open": market_open,
    }

    # 取得 df 最後一筆 K 棒的日期（盤後判斷快取新舊用）
    try:
        last_idx = df.index[-1]
        data_date = (last_idx.strftime("%Y-%m-%d")
                     if hasattr(last_idx, 'strftime')
                     else str(last_idx)[:10])
    except Exception:
        data_date = ""

    # 寫入 L1 cache
    signals_cache[f"signals_{symbol}"] = {
        "data": result_data,
        "fetched_at": time.time(),
        "data_date": data_date,
    }
    # 記錄台股更新時間
    last_update_timestamps["tw_stock"] = datetime.now().strftime("%H:%M:%S")
    return result_data


def _fetch_yfinance_df(symbol: str):
    """用 yfinance 抓取台股日線 DataFrame（自動嘗試 .TW 和 .TWO）。
    回傳格式與 fetch_twse_daily 相容（index=date, columns=open/high/low/close/volume）。
    """
    base = symbol.split('.')[0] if '.' in symbol else symbol
    for suffix in ['.TW', '.TWO']:
        yf_sym = base + suffix
        try:
            ticker = yf.Ticker(yf_sym)
            df = ticker.history(period='1y', interval='1d')
            if df.empty or len(df) < 30:
                continue
            df = df.rename(columns={
                'Open': 'open', 'High': 'high', 'Low': 'low',
                'Close': 'close', 'Volume': 'volume',
            })[['open', 'high', 'low', 'close', 'volume']]
            df.index.name = 'date'
            df.index = df.index.tz_localize(None)
            print(f"[yfinance-df] {yf_sym}: {len(df)} rows")
            return df
        except Exception as e:
            print(f"[yfinance-df] {yf_sym} failed: {e}")
    return None


def _fetch_tw_df(symbol: str, market: str):
    """嘗試從 TWSE → yfinance 抓取台股資料，成功後存入本地 CSV（L2）。尊重 rate limit。"""
    global tw_last_real_fetch
    if market == 'futures':
        return None
    if not tw_can_fetch_now():
        return None
    # 優先 TWSE（上市股）
    df = fetch_twse_daily(symbol, limit=200, months=12)
    # TWSE 無資料 → yfinance fallback（支援上櫃股 .TWO）
    if df is None or len(df) < 30:
        df = _fetch_yfinance_df(symbol)
    if df is not None and len(df) >= 30:
        # 記錄 rate limit 時間戳
        tw_last_real_fetch = time.time()
        _save_tw_rate_state()
        # 存入本地 CSV (L2 cache)
        save_local_history(symbol, df)
        return df
    return None


@app.get("/api/tw-signals")
async def get_tw_signals(symbol: str, market: str = "stock"):
    """台股/期貨技術信號，三層快取：L1 記憶體 → L2 本地 CSV → L3 TWSE API。"""
    cache_key = f"signals_{symbol}"
    now = time.time()
    market_open = is_tw_market_open()
    remaining = tw_seconds_until_next() if market_open else None

    # --- L1: 記憶體快取 ---
    if cache_key in signals_cache:
        cached = signals_cache[cache_key]
        age = now - cached["fetched_at"]
        cached_data_date = cached.get("data_date", "")
        latest_trading_day = latest_closed_tw_trading_day()
        # 盤中：60 秒內 TTL
        # 盤後：資料日期 == 最近收盤日才視為新鮮，否則重抓（避免跨日後仍回舊快取）
        is_fresh_intraday = market_open and age < TW_RATE_LIMIT_SEC
        is_fresh_overnight = (not market_open) and cached_data_date and cached_data_date >= latest_trading_day
        if is_fresh_intraday or is_fresh_overnight:
            print(f"[signals L1] {symbol} (age={int(age)}s, open={market_open}, data_date={cached_data_date})")
            result = dict(cached["data"])
            result["next_update_in"] = remaining
            result["data_source"] = cached["data"]["data_source"] + ("" if market_open else "_closed")
            return result
        else:
            print(f"[signals L1 stale] {symbol} data_date={cached_data_date} < latest={latest_trading_day}, refetch")

    # --- L3: TWSE API (盤中可抓，盤後只在無任何快取時抓一次；同步 IO 丟 thread pool) ---
    df = await asyncio.to_thread(_fetch_tw_df, symbol, market)
    if df is not None:
        return _analyze_tw_df(symbol, market, df, "twse_daily")

    # --- L2: 本地 CSV ---
    local_df = await asyncio.to_thread(load_local_history, symbol)
    if local_df is not None and len(local_df) >= 30:
        print(f"[signals L2] {symbol} from local CSV ({len(local_df)} rows)")
        src = "local_csv" + ("" if market_open else "_closed")
        return _analyze_tw_df(symbol, market, local_df, src)

    # --- 盤後無任何資料，嘗試強制抓一次（TWSE → yfinance，忽略 rate limit） ---
    if not market_open and market != 'futures':
        print(f"[signals] No cache for {symbol}, one-time fetch for after-hours...")
        df = await asyncio.to_thread(fetch_twse_daily, symbol, 200, 12)
        if df is None or len(df) < 30:
            df = await asyncio.to_thread(_fetch_yfinance_df, symbol)
        if df is not None and len(df) >= 30:
            await asyncio.to_thread(save_local_history, symbol, df)
            return _analyze_tw_df(symbol, market, df, "twse_daily_closed")

    return {"symbol": symbol, "signals": {}, "next_update_in": remaining, "data_source": "no_data", "market_open": market_open}


@app.get("/api/ticker-summary")
async def get_ticker_summary():
    """頁面載入時一次取得所有 ticker 資料（crypto 從記憶體，台股從快取/本地/API）。"""
    result = {"crypto": {}, "tw": {}, "crypto_updated_at": last_update_timestamps["crypto"], "tw_updated_at": last_update_timestamps["tw_stock"]}

    # Crypto: 直接從 current_signals 取
    for sym, data in current_signals.items():
        sigs = data.get("signals", {})
        d1 = sigs.get("1d")
        if d1:
            result["crypto"][sym] = {
                "price": d1.get("price"),
                "confidence": d1.get("confidence"),
                "change_24h": d1.get("change_24h", 0),
            }

    # TW: 嘗試從 L1 cache → L2 local CSV → L3 TWSE API
    for tw_sym, tw_market in TW_TICKER_SYMBOLS:
        cache_key = f"signals_{tw_sym}"
        if cache_key in signals_cache:
            cached_data = signals_cache[cache_key]["data"]
            d1 = cached_data.get("signals", {}).get("1d")
            if d1:
                result["tw"][tw_sym] = {
                    "price": d1.get("price"),
                    "confidence": d1.get("confidence"),
                    "change_24h": d1.get("change_24h", 0),
                }
                continue

        # L2: 本地 CSV（同步 IO 丟 thread pool 避免阻塞 event loop）
        local_df = await asyncio.to_thread(load_local_history, tw_sym)
        if local_df is not None and len(local_df) >= 30:
            sig_result = _analyze_tw_df(tw_sym, tw_market, local_df, "local_csv")
            d1 = sig_result.get("signals", {}).get("1d")
            if d1:
                result["tw"][tw_sym] = {
                    "price": d1.get("price"),
                    "confidence": d1.get("confidence"),
                    "change_24h": d1.get("change_24h", 0),
                }
                continue

        # L3: TWSE API（尊重 rate limit）
        if tw_market != 'futures':
            df = await asyncio.to_thread(_fetch_tw_df, tw_sym, tw_market)
            if df is not None:
                sig_result = _analyze_tw_df(tw_sym, tw_market, df, "twse_daily")
                d1 = sig_result.get("signals", {}).get("1d")
                if d1:
                    result["tw"][tw_sym] = {
                        "price": d1.get("price"),
                        "confidence": d1.get("confidence"),
                        "change_24h": d1.get("change_24h", 0),
                    }
                    continue

            # 最後手段：完全無資料，強制抓一次 TWSE（僅此一次；同步 IO 丟 thread pool）
            if tw_sym not in result["tw"]:
                print(f"[ticker-summary] {tw_sym} no data, one-time forced fetch...")
                forced_df = await asyncio.to_thread(fetch_twse_daily, tw_sym, 200, 12)
                if forced_df is not None and len(forced_df) >= 30:
                    await asyncio.to_thread(save_local_history, tw_sym, forced_df)
                    sig_result = _analyze_tw_df(tw_sym, tw_market, forced_df, "twse_forced")
                    d1 = sig_result.get("signals", {}).get("1d")
                    if d1:
                        result["tw"][tw_sym] = {
                            "price": d1.get("price"),
                            "confidence": d1.get("confidence"),
                            "change_24h": d1.get("change_24h", 0),
                        }

    result["tw_updated_at"] = last_update_timestamps["tw_stock"]
    return result


@app.get("/api/update-status")
async def get_update_status():
    """回傳 crypto / tw 各自的最新更新時間。"""
    return {
        "crypto_updated_at": last_update_timestamps["crypto"],
        "tw_updated_at": last_update_timestamps["tw_stock"],
        "tw_market_open": is_tw_market_open(),
        "tw_next_fetch_in": tw_seconds_until_next() if is_tw_market_open() else None,
    }


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
chart_cache: dict = {}
signals_cache: dict = {}

# --- 持久化 Rate Limiter ---
def _load_tw_rate_state() -> float:
    """從磁碟讀取上一次真實請求的時間戳（重啟也不歸零）。"""
    try:
        with open(TW_RATE_STATE_PATH, 'r') as f:
            state = json.load(f)
            return float(state.get("tw_last_real_fetch", 0.0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0.0

def _save_tw_rate_state():
    """將最後請求時間戳寫入磁碟。"""
    os.makedirs(os.path.dirname(TW_RATE_STATE_PATH), exist_ok=True)
    try:
        with open(TW_RATE_STATE_PATH, 'w') as f:
            json.dump({"tw_last_real_fetch": tw_last_real_fetch}, f)
    except Exception as e:
        print(f"[rate-state] Save error: {e}")

tw_last_real_fetch: float = _load_tw_rate_state()

def tw_can_fetch_now() -> bool:
    return (time.time() - tw_last_real_fetch) >= TW_RATE_LIMIT_SEC

def tw_seconds_until_next() -> int:
    elapsed = time.time() - tw_last_real_fetch
    remaining = max(0, TW_RATE_LIMIT_SEC - elapsed)
    return int(remaining)

# --- 本地 CSV 歷史快取（L2 cache） ---
def _safe_filename(symbol: str) -> str:
    return symbol.replace("/", "_").replace(".", "_")

def load_local_history(symbol: str):
    """從本地 CSV 讀取歷史資料，回傳 DataFrame 或 None。"""
    path = os.path.join(HISTORY_DIR, f"{_safe_filename(symbol)}.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path)
        # 支援 time (unix) 或 date 欄位
        if 'time' in df.columns:
            df['date'] = pd.to_datetime(df['time'], unit='s')
        elif 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
        else:
            return None
        df.set_index('date', inplace=True)
        # 確保有必要的欄位
        for col in ['open', 'high', 'low', 'close']:
            if col not in df.columns:
                return None
        return df
    except Exception as e:
        print(f"[local-history] Read error ({symbol}): {e}")
        return None

def save_local_history(symbol: str, df):
    """將 DataFrame 寫入本地 CSV。"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"{_safe_filename(symbol)}.csv")
    try:
        out = df.copy()
        out.to_csv(path)
        print(f"[local-history] Saved {len(out)} rows -> {path}")
    except Exception as e:
        print(f"[local-history] Save error ({symbol}): {e}")

# --- 更新時間戳追蹤 ---
last_update_timestamps = {
    "crypto": None,    # ISO string
    "tw_stock": None,  # ISO string
}

# 預設的台股 ticker 標的（各類股代表）
TW_TICKER_SYMBOLS = [
    ("2330.TW", "stock"),   # 半導體 - 台積電
    ("0050.TW", "stock"),   # 大盤 ETF - 元大台灣50
    ("2317.TW", "stock"),   # 電子代工 - 鴻海
    ("2881.TW", "stock"),   # 金融 - 富邦金
    ("2603.TW", "stock"),   # 航運 - 長榮
]


_VALID_TW_SYMBOL = re.compile(r'^[A-Z0-9]+\.(TW|TWO)$')


def _collect_active_universe() -> List[Tuple[str, str]]:
    """收集所有需保持資料新鮮的台股標的（去重）。
    來源：跑馬燈 ticker ∪ 6 個產業池 ∪ Screener 自選股。
    過濾不合法 symbol（避免歷史垃圾資料拖慢刷新）。
    """
    universe: Dict[str, str] = {}  # symbol → market

    for sym, mkt in TW_TICKER_SYMBOLS:
        universe[sym] = mkt

    try:
        from sector_trader import SECTOR_STOCKS
        for stocks in SECTOR_STOCKS.values():
            for sym in stocks:
                universe.setdefault(sym, "stock")
    except Exception as e:
        print(f"[universe] sector_trader load failed: {e}")

    try:
        from screener import SCREENER_UNIVERSE
        for sym in SCREENER_UNIVERSE:
            universe.setdefault(sym, "stock")
    except Exception as e:
        print(f"[universe] screener load failed: {e}")

    valid = [(sym, mkt) for sym, mkt in universe.items() if _VALID_TW_SYMBOL.match(sym)]
    skipped = [sym for sym in universe if not _VALID_TW_SYMBOL.match(sym)]
    if skipped:
        print(f"[universe] skipped invalid symbols: {skipped}")
    return valid


# 過期 CSV 背景刷新佇列：B2 路徑發現過期就排隊，由 stale_refresh_worker 消化
_stale_refresh_pending: set = set()


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


def latest_closed_tw_trading_day() -> str:
    """回傳「最近一個已收盤交易日」的日期字串 YYYY-MM-DD。

    判定：
    - 週一~五 14:30 後 → 今天
    - 週一~五 14:30 前 → 上一個交易日
    - 週末 → 上一個週五（簡化處理，不考慮國定假日）
    """
    tz = pytz.timezone('Asia/Taipei')
    now = datetime.now(tz)
    candidate = now.date()

    today_close = now.replace(hour=14, minute=30, second=0, microsecond=0)
    if now.weekday() < 5 and now >= today_close:
        return candidate.strftime("%Y-%m-%d")

    # 否則回推到最近的工作日
    candidate = candidate - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate - timedelta(days=1)
    return candidate.strftime("%Y-%m-%d")

@app.get("/api/ping")
async def ping():
    return {"status": "ok", "server_time": time.time()}

def fetch_yfinance_candles(symbol: str, timeframe: str, limit: int = 200):
    """不帶快取、直接向 yfinance 抓資料，回傳 (candles_list, source_str)。"""
    global tw_last_real_fetch

    base = symbol.split('.')[0] if '.' in symbol else symbol
    yf_interval = "1d"
    period = "1y"
    if timeframe in ["1m", "5m", "15m", "30m", "60m", "1h", "4h"]:
        yf_interval = "60m" if timeframe in ["1h", "4h"] else timeframe
        period = "1mo"

    # 自動嘗試 .TW（上市）和 .TWO（上櫃）
    suffixes = ['.TW', '.TWO'] if '.' not in symbol or symbol.endswith('.TW') else [symbol.split('.', 1)[1]]
    df = None
    yf_symbol = None
    for suffix in suffixes:
        yf_symbol = base + suffix
        try:
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(period=period, interval=yf_interval)
            if not df.empty:
                break
            df = None
        except Exception:
            df = None

    print(f"[yfinance] Fetching {yf_symbol} ({timeframe}) -> {'OK' if df is not None else 'empty'}")
    try:
        if df is None or df.empty:
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

        # 記錄本次真實請求時間（並持久化到磁碟）
        tw_last_real_fetch = time.time()
        _save_tw_rate_state()
        return candles, "yfinance"
    except Exception as e:
        print(f"[yfinance] Error for {symbol}: {e}")
        return None, None


def get_tw_chart_data(symbol: str, timeframe: str, limit: int = 200):
    """
    台股走勢圖資料取得（帶嚴格全域 Rate Limit + Cache）。
    """
    global tw_last_real_fetch
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

    # B2: 沒有快取 + 日線 → 先找本地 CSV，無資料才抓 TWSE
    if timeframe == "1d":
        local_df = load_local_history(symbol)
        if local_df is not None and len(local_df) >= 30:
            # CSV 過期 (>4 天) 排隊背景刷新；當下仍回傳舊資料避免阻塞
            _maybe_queue_stale_refresh(symbol, local_df)
            candles = [{"time": int(idx.timestamp()), "open": float(row['open']), "high": float(row['high']),
                         "low": float(row['low']), "close": float(row['close']), "volume": float(row.get('volume', 0) or 0)}
                        for idx, row in local_df.iterrows()]
            chart_cache[cache_key] = {"candles": candles, "fetched_at": now, "source": "local_csv"}
            return {"candles": candles, "data_source": "local_csv" + src_suffix, "fetched_at": now, "next_update_in": remaining}

        # 本地也沒有，才抓 TWSE（並更新 rate limit）
        df = fetch_twse_daily(symbol, limit=limit, months=24)
        if df is not None:
            tw_last_real_fetch = time.time()
            _save_tw_rate_state()
            save_local_history(symbol, df)
            candles = [{"time": int(idx.timestamp()), "open": float(row['open']), "high": float(row['high']),
                         "low": float(row['low']), "close": float(row['close']), "volume": float(row.get('volume', 0) or 0)}
                        for idx, row in df.iterrows()]
            chart_cache[cache_key] = {"candles": candles, "fetched_at": now, "source": "twse_daily"}
            return {"candles": candles, "data_source": "twse_daily" + src_suffix, "fetched_at": now, "next_update_in": TW_RATE_LIMIT_SEC}

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
        result = await asyncio.to_thread(get_tw_chart_data, symbol, timeframe, 200)
        if result and result["candles"]:
            return {
                "candles": result["candles"],
                "data_source": result["data_source"],
                "next_update_in": result["next_update_in"]
            }
        return {"candles": [], "data_source": None, "next_update_in": 0}

    if market == 'stock':
        result = await asyncio.to_thread(get_tw_chart_data, symbol, timeframe, 200)
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


# ============================================================
# 類股虛擬交易 API（6 個獨立交易中心）
# ============================================================

from sector_trader import get_manager, get_all_managers as get_all_sector_managers, SECTOR_IDS, SECTOR_ID_TO_NAME
from sector_auto_trader import auto_trader as sector_auto_trader

@app.get("/api/sector-trading/sectors")
async def list_sectors():
    """列出所有類股及其摘要"""
    from sector_auto_trader import get_current_price
    results = []
    for sector_id, mgr in get_all_sector_managers().items():
        current_prices = {}
        for symbol, hold in mgr.state.get("holdings", {}).items():
            if hold.get("qty", 0) > 0:
                price = get_current_price(symbol)
                if price:
                    current_prices[symbol] = price
        results.append(mgr.get_summary(current_prices))
    return results

# ── 自動交易守護程式控制（必須在 {sector_id} 路由之前）──

@app.post("/api/sector-trading/auto-trader/start")
async def start_auto_trader():
    """啟動背景自動交易"""
    ok = sector_auto_trader.start()
    return {"started": ok, **sector_auto_trader.get_status()}

@app.post("/api/sector-trading/auto-trader/stop")
async def stop_auto_trader():
    """停止背景自動交易"""
    ok = sector_auto_trader.stop()
    return {"stopped": ok, **sector_auto_trader.get_status()}

@app.get("/api/sector-trading/auto-trader/status")
async def get_auto_trader_status():
    """取得自動交易狀態"""
    return sector_auto_trader.get_status()

@app.post("/api/sector-trading/auto-trader/run-once")
async def run_auto_trader_once():
    """手動觸發一次交易檢查"""
    import threading
    t = threading.Thread(target=sector_auto_trader.run_once_now, daemon=True)
    t.start()
    return {"triggered": True, "message": "已觸發一次交易檢查，請稍後查看結果"}

# ════════════════════════════════════════════════════
# BTC 自動交易 API
# ════════════════════════════════════════════════════

@app.get("/api/btc-trading/status")
async def btc_trading_status():
    """取得 BTC 交易帳戶狀態"""
    from btc_auto_trader import btc_trader
    return btc_trader.get_status()

@app.post("/api/btc-trading/toggle")
async def btc_trading_toggle(active: bool = True):
    """開啟/關閉 BTC 自動交易"""
    from btc_auto_trader import btc_trader
    btc_trader.account.toggle(active)
    if active and not btc_trader.is_running:
        btc_trader.start()
    return {"is_active": active, "message": f"BTC 自動交易已{'開啟' if active else '關閉'}"}

@app.post("/api/btc-trading/run-once")
async def btc_trading_run_once():
    """手動觸發一次 BTC 交易檢查"""
    import threading
    from btc_auto_trader import btc_trader
    if not btc_trader.account.is_active:
        return {"error": "BTC 交易未啟用，請先開啟"}
    t = threading.Thread(target=btc_trader.run_once, daemon=True)
    t.start()
    return {"triggered": True, "message": "已觸發 BTC 交易檢查"}

@app.post("/api/daily-report/send")
async def trigger_daily_report():
    """手動觸發每日績效報告"""
    import threading
    from daily_report import send_daily_report
    t = threading.Thread(target=send_daily_report, daemon=True)
    t.start()
    return {"triggered": True, "message": "績效報告已觸發，稍後將發送至 Telegram"}

@app.get("/api/btc-trading/history")
async def btc_trading_history():
    """取得 BTC 交易歷史"""
    from btc_auto_trader import btc_trader
    return btc_trader.account.state.get("history", [])[:50]

@app.get("/api/btc-trading/equity-curve")
async def btc_equity_curve():
    """取得 BTC 權益曲線"""
    from btc_auto_trader import btc_trader
    return btc_trader.account.state.get("equity_curve", [])

@app.get("/api/btc-trading/flow-info")
async def btc_flow_info():
    """取得最新恐懼貪婪指數與資金費率"""
    try:
        from layers.crypto_flow import CryptoFlowLayer
        import pandas as pd
        layer = CryptoFlowLayer()
        layer._load_data()
        now = pd.Timestamp.now()
        fng = layer._get_fng(now)
        fr_pct = layer._get_funding_rate_percentile(now)
        # 取得 fng class
        if fng <= 25:
            fng_class = "極度恐懼"
        elif fng <= 45:
            fng_class = "恐懼"
        elif fng <= 55:
            fng_class = "中性"
        elif fng <= 75:
            fng_class = "貪婪"
        else:
            fng_class = "極度貪婪"
        return {"fear_greed": fng, "fng_class": fng_class, "funding_rate_pct": fr_pct}
    except Exception as e:
        return {"fear_greed": 50, "fng_class": "N/A", "funding_rate_pct": 50, "error": str(e)}

# ── 類股個別操作 ──

@app.get("/api/sector-trading/{sector_id}/status")
async def get_sector_status(sector_id: str):
    """取得單一類股帳戶摘要"""
    mgr = get_manager(sector_id)
    if not mgr:
        return {"error": f"未知的類股 ID: {sector_id}"}
    # 統一取價：多來源比較日期，取最新的收盤價
    from sector_auto_trader import get_current_price
    current_prices = {}
    for symbol, hold in mgr.state.get("holdings", {}).items():
        if hold.get("qty", 0) > 0:
            price = get_current_price(symbol)
            if price:
                current_prices[symbol] = price
    return mgr.get_summary(current_prices)

@app.post("/api/sector-trading/{sector_id}/toggle")
async def toggle_sector_trading(sector_id: str, active: bool = False):
    """啟動/停止單一類股自動交易"""
    mgr = get_manager(sector_id)
    if not mgr:
        return {"error": f"未知的類股 ID: {sector_id}"}
    is_active = mgr.toggle_active(active)
    return {"sector_id": sector_id, "is_active": is_active}

@app.get("/api/sector-trading/{sector_id}/history")
async def get_sector_history(sector_id: str, page: int = 1, pageSize: int = 50,
                              symbol: str = "", startDate: str = "", endDate: str = "",
                              tradeType: str = ""):
    """取得單一類股交易歷史"""
    mgr = get_manager(sector_id)
    if not mgr:
        return {"error": f"未知的類股 ID: {sector_id}"}
    # 取得目前持倉的即時價格，用於計算未實現損益
    from sector_auto_trader import get_current_price
    current_prices = {}
    for sym, hold in mgr.state.get("holdings", {}).items():
        if hold.get("qty", 0) > 0:
            price = get_current_price(sym)
            if price:
                current_prices[sym] = price
    return mgr.get_history(page, pageSize, symbol, startDate, endDate,
                           trade_type=tradeType,
                           current_prices=current_prices)

@app.post("/api/sector-trading/{sector_id}/strategy")
async def update_sector_strategy(sector_id: str, strategy: dict):
    """更新類股策略設定"""
    mgr = get_manager(sector_id)
    if not mgr:
        return {"error": f"未知的類股 ID: {sector_id}"}
    mgr.update_strategy(strategy)
    return {"success": True, "strategy": mgr.get_strategy()}

@app.post("/api/sector-trading/{sector_id}/reset")
async def reset_sector_account(sector_id: str):
    """重置類股帳戶（保留策略）"""
    mgr = get_manager(sector_id)
    if not mgr:
        return {"error": f"未知的類股 ID: {sector_id}"}
    mgr.reset_account()
    return {"success": True}

def _sanitize(obj):
    """將 numpy 類型轉為 Python 原生類型，避免 JSON 序列化錯誤"""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    elif isinstance(obj, (np.bool_,)):
        return bool(obj)
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj

def _compute_sector_regime(sector_id: str):
    """同步計算類股盤勢辨識；給 to_thread 用，避免阻塞 event loop。"""
    mgr = get_manager(sector_id)
    if not mgr:
        return {"error": f"未知的類股 ID: {sector_id}"}

    from sector_auto_trader import fetch_signal_data, build_layers
    from signals.aggregator import SignalAggregator

    strategy = mgr.get_strategy()
    layers = build_layers(strategy)
    results = {}

    for symbol in mgr.state.get("stocks", []):
        df = fetch_signal_data(symbol)
        if df is None:
            results[symbol] = {"regime": "無數據", "details": {}}
            continue

        aggregator = SignalAggregator(weights=strategy["weights"])
        signal = aggregator.analyze(
            df.copy(), symbol, "1d",
            layers=layers, sector_id=sector_id,
        )

        modifier = signal.layer_modifiers[0] if signal.layer_modifiers else None
        details = _sanitize(modifier.details) if modifier else {}
        results[symbol] = {
            "name": mgr.stocks.get(symbol, symbol),
            "price": round(float(df['close'].iloc[-1]), 2),
            "regime": signal.regime or "未知",
            "buy_score": round(float(signal.buy_score), 1),
            "sell_score": round(float(signal.sell_score), 1),
            "raw_buy_score": round(float(signal.raw_buy_score), 1),
            "raw_sell_score": round(float(signal.raw_sell_score), 1),
            "direction": signal.direction,
            "signal_level": signal.signal_level,
            "details": details,
            "reason": modifier.reason if modifier else "",
        }

    return {"sector_id": sector_id, "stocks": results}


@app.get("/api/sector-trading/{sector_id}/regime")
async def get_sector_regime(sector_id: str):
    """取得類股各標的即時盤勢辨識"""
    return await asyncio.to_thread(_compute_sector_regime, sector_id)


@app.get("/api/stock-lookup")
async def stock_lookup(q: str):
    """用中文名稱或代碼模糊搜尋台股，回傳匹配的 symbol 清單（最多 10 筆）"""
    from layers.fundamental import fetch_twse_pe_all
    from sector_trader import SECTOR_STOCKS
    q = q.strip()
    if not q:
        return []

    results = []
    # 1. 先查交易中心追蹤清單（精確優先）
    for sector, stocks in SECTOR_STOCKS.items():
        for sym, name in stocks.items():
            code = sym.replace(".TW", "").replace(".TWO", "")
            if q == name or q == code or q == sym:
                return [{"symbol": sym, "name": name, "sector": sector}]
            if q in name or q in code:
                results.append({"symbol": sym, "name": name, "sector": sector})

    # 2. 再查全市場 TWSE 資料
    if len(results) < 10:
        all_pe = fetch_twse_pe_all()
        for code, info in all_pe.items():
            name = info.get("name", "")
            sym = code + ".TW"
            if any(r["symbol"] == sym for r in results):
                continue
            if q == name or q == code:
                results.insert(0, {"symbol": sym, "name": name, "sector": None})
            elif q in name or q in code:
                results.append({"symbol": sym, "name": name, "sector": None})
            if len(results) >= 10:
                break
    return results[:10]


@app.get("/api/stock-analysis")
async def get_stock_analysis(symbol: str):
    """
    單一股票四面分析：技術面 + 基本面 P/E + 盤勢辨識 + 籌碼面 + 消息面
    用於首頁查詢台股時一次揭露完整資訊。
    """
    from layers.fundamental import fetch_twse_pe_all, FundamentalLayer, _strip_tw, compute_fundamental_score
    from layers.regime import RegimeLayer
    from layers.sentiment import get_stock_sentiment, get_market_sentiment, fetch_rss_articles
    from layers.chipflow import fetch_chip_summary, compute_chip_score
    from sector_auto_trader import fetch_signal_data
    from signals.aggregator import SignalAggregator
    import pandas as pd

    result = {"symbol": symbol, "fundamental": None, "regime": None, "technical": None, "chipflow": None}

    # ── 1. 基本面（成長/價值雙軌） ──
    fund_buy_score = 50
    all_pe = fetch_twse_pe_all()
    code = _strip_tw(symbol)
    if all_pe and code in all_pe:
        info = all_pe[code]
        pe = info.get("pe")
        dy = info.get("dy")
        pb = info.get("pb")

        from layers.fundamental import fetch_twse_revenue_all, get_sector_pe_stats
        all_rev = fetch_twse_revenue_all()
        rev_info = all_rev.get(code, {})
        mom = rev_info.get("mom")
        yoy = rev_info.get("yoy")
        sector = rev_info.get("sector")

        # 產業百分位
        sector_pe_median = None
        pe_percentile = None
        valuation = "無數據"
        if sector and pe is not None and pe > 0:
            same_sector_symbols = [f"{c}.TW" for c, v in all_rev.items() if v.get("sector") == sector]
            if len(same_sector_symbols) >= 3:
                pe_stats = get_sector_pe_stats(same_sector_symbols, all_pe)
                sym_key = f"{code}.TW"
                if sym_key in pe_stats:
                    stat = pe_stats[sym_key]
                    pe_percentile = stat.get("percentile")
                    sector_pe_median = stat.get("sector_median_pe")
                    valuation = stat.get("valuation", "無數據")

        # 統一評分函數
        fund_result = compute_fundamental_score(
            pe=pe, dy=dy, yoy=yoy, mom=mom, pe_percentile=pe_percentile)
        fund_buy_score = fund_result["score"]
        fund_advice = fund_result["advice"]

        result["fundamental"] = {
            "pe": pe, "dy": dy, "pb": pb,
            "mom": mom, "yoy": yoy,
            "sector": sector, "sector_pe_median": sector_pe_median, "pe_percentile": pe_percentile,
            "name": info.get("name", ""),
            "valuation": valuation,
            "buy_score": int(fund_buy_score),
            "advice": fund_advice,
            "peg": fund_result["peg"],
            "track": fund_result["track"],
        }

    # ── 2. 盤勢辨識 + 技術面摘要 ──
    tech_buy_score = 50
    regime_buy_score = 50
    df = fetch_signal_data(symbol)
    if df is not None and len(df) >= 120:
        regime_layer = RegimeLayer(enabled=True)
        modifier = regime_layer.compute_modifier(symbol, df)
        details = _sanitize(modifier.details) if modifier.details else {}

        # 盤勢做多分數
        regime_state = modifier.regime or "未知"
        regime_scores = {
            "強勢多頭": 90, "多頭": 75, "底部轉強": 70,
            "盤整": 50, "高檔轉折": 25, "空頭": 15,
        }
        regime_buy_score = regime_scores.get(regime_state, 50)

        # 傳產 Regime Veto-Only：回測顯示多頭加乘在循環股（航運等）有害
        from screener import get_symbol_sector
        if get_symbol_sector(symbol) == "traditional" and regime_state in ("強勢多頭", "多頭"):
            regime_buy_score = min(regime_buy_score, 60)

        regime_advices = {
            "強勢多頭": "趨勢強勁，順勢做多",
            "多頭": "多頭格局，適合持有或加碼",
            "底部轉強": "底部轉強訊號，可分批布局",
            "盤整": "方向不明，建議觀望或輕倉",
            "高檔轉折": "高檔出現轉弱訊號，不宜追高",
            "空頭": "空頭趨勢，建議觀望不進場",
        }

        result["regime"] = {
            "state": regime_state,
            "reason": modifier.reason,
            "confidence": details.get("confidence", 0),
            "trend": details.get("trend", {}),
            "ma_alignment": details.get("ma_alignment", {}),
            "position": details.get("position", {}),
            "kline_pattern": details.get("kline_pattern", {}),
            "volume_pattern": details.get("volume_pattern", {}),
            "buy_score": regime_buy_score,
            "advice": regime_advices.get(regime_state, ""),
        }

        # 技術面指標摘要（按產業使用回測最佳權重）
        from screener import get_sector_weights, get_symbol_sector
        sector_weights = get_sector_weights(symbol)
        agg = SignalAggregator(weights=sector_weights)
        signal = agg.analyze(df.copy(), symbol, "1d")
        tech_buy_score = round(float(signal.buy_score), 1)

        # 做多建議文字
        if signal.direction == "BUY" and signal.confidence >= 70:
            tech_advice = "技術指標強勢看多，適合進場"
        elif signal.direction == "BUY":
            tech_advice = "技術面偏多，可留意買點"
        elif signal.direction == "SELL" and signal.confidence >= 70:
            tech_advice = "技術面轉弱，建議觀望或減碼"
        elif signal.direction == "SELL":
            tech_advice = "技術面偏弱，暫不建議進場"
        else:
            tech_advice = "技術面中性，靜待方向明朗"

        result["technical"] = {
            "buy_score": tech_buy_score,
            "sell_score": round(float(signal.sell_score), 1),
            "direction": signal.direction,
            "confidence": round(float(signal.confidence), 1),
            "signal_level": signal.signal_level,
            "advice": tech_advice,
        }

    # ── 3. 籌碼面分析（用 to_thread 避免阻塞 event loop）──
    import asyncio
    chip_buy_score = None
    try:
        chip_summary = await asyncio.to_thread(fetch_chip_summary, symbol)
        if chip_summary:
            chip = compute_chip_score(chip_summary)
            chip_buy_score = chip["score"]

            # 外資/投信連買天數文字
            fc = chip_summary.get("foreign_consec_buy", 0)
            tc = chip_summary.get("trust_consec_buy", 0)
            foreign_text = f"連買{fc}天" if fc > 0 else (f"連賣{abs(fc)}天" if fc < 0 else "持平")
            trust_text = f"連買{tc}天" if tc > 0 else (f"連賣{abs(tc)}天" if tc < 0 else "持平")

            result["chipflow"] = {
                "status": "active",
                "buy_score": chip_buy_score,
                "label": chip["label"],
                "advice": chip["advice"],
                "foreign_consec_buy": fc,
                "foreign_text": foreign_text,
                "foreign_total_net": chip_summary.get("foreign_total_net", 0),
                "trust_consec_buy": tc,
                "trust_text": trust_text,
                "trust_total_net": chip_summary.get("trust_total_net", 0),
                "dealer_total_net": chip_summary.get("dealer_total_net", 0),
                "margin_change_sum": chip_summary.get("margin_change_sum", 0),
                "short_balance_latest": chip_summary.get("short_balance_latest", 0),
                "sub_scores": chip["sub_scores"],
                "latest_date": chip_summary.get("latest_date", ""),
                "days_analyzed": chip_summary.get("days_analyzed", 0),
                # 近 30 日累計
                "foreign_30d_net": chip_summary.get("foreign_30d_net", 0),
                "trust_30d_net": chip_summary.get("trust_30d_net", 0),
                "dealer_30d_net": chip_summary.get("dealer_30d_net", 0),
                "margin_30d_change": chip_summary.get("margin_30d_change", 0),
                "short_30d_change": chip_summary.get("short_30d_change", 0),
                "days_30d_analyzed": chip_summary.get("days_30d_analyzed", 0),
                "daily_data": chip_summary.get("daily_data", []),
            }
    except Exception as e:
        print(f"⚠️ 籌碼面分析失敗: {e}")
        result["chipflow"] = {"status": "error", "buy_score": None, "message": str(e)}

    # ── 5. 消息面情緒分析 ──
    sent_buy_score = None
    try:
        articles = fetch_rss_articles()
        stock_name = result.get("fundamental", {}).get("name", "") if result.get("fundamental") else ""
        sentiment = get_stock_sentiment(symbol, stock_name, articles)
        market_sent = get_market_sentiment(articles)

        # 情緒做多分數（0~100）
        raw_sent = sentiment["score"]  # -100 ~ +100
        
        # 如果完全沒有相關新聞，將分數設為 None (即不參與綜合評分計算，避免被預設 50 分拉低整體的評等)
        if sentiment["total_related"] == 0:
            sent_buy_score = None
            sentiment["advice"] += " (無新聞，不列入綜合評分)"
        else:
            sent_buy_score = round(max(0, min(100, 50 + raw_sent * 0.5)), 1)

        result["sentiment"] = {
            "status": "active",
            "buy_score": sent_buy_score,
            "score": sentiment["score"],
            "label": sentiment["sentiment_label"],
            "advice": sentiment["advice"],
            "positive_count": sentiment["positive_count"],
            "negative_count": sentiment["negative_count"],
            "neutral_count": sentiment["neutral_count"],
            "total_related": sentiment["total_related"],
            "recent_news": sentiment["recent_news"],
            "market": {
                "score": market_sent["score"],
                "label": market_sent["label"],
                "positive_pct": market_sent.get("positive_pct", 0),
            },
        }
    except Exception as e:
        print(f"⚠️ 消息面分析失敗: {e}")
        result["sentiment"] = {"status": "error", "buy_score": None, "message": str(e)}

    # ── 6. 綜合做多建議（按產業使用不同五維權重）──
    scores = []
    from screener import get_symbol_sector, SECTOR_COMPOSITE_WEIGHTS
    _sector = get_symbol_sector(symbol)
    score_weights = SECTOR_COMPOSITE_WEIGHTS.get(_sector, SECTOR_COMPOSITE_WEIGHTS["default"])
    for key, w in score_weights.items():
        layer = result.get(key)
        if layer and layer.get("buy_score") is not None:
            scores.append((float(layer["buy_score"]), w))

    if scores:
        total_w = sum(w for _, w in scores)
        composite = sum(s * w for s, w in scores) / total_w
        composite = round(composite, 1)

        if composite >= 75:
            action = "積極買進"
            action_cls = "strong_buy"
        elif composite >= 60:
            action = "建議買進"
            action_cls = "buy"
        elif composite >= 45:
            action = "中性觀望"
            action_cls = "neutral"
        elif composite >= 30:
            action = "偏空觀望"
            action_cls = "weak"
        else:
            action = "不建議進場"
            action_cls = "avoid"

        # 只回傳實際參與計算的權重（重新分配後）
        actual_keys = set()
        for key, w in score_weights.items():
            layer = result.get(key)
            if layer and layer.get("buy_score") is not None:
                actual_keys.add(key)
        actual_weights = {k: v for k, v in score_weights.items() if k in actual_keys}
        actual_total = sum(actual_weights.values()) or 1
        normalized_weights = {k: round(v / actual_total * 100) for k, v in actual_weights.items()}

        result["recommendation"] = {
            "composite_score": composite,
            "action": action,
            "action_class": action_cls,
            "weights": normalized_weights,
            "sector": _sector,
        }
    else:
        result["recommendation"] = {
            "composite_score": None,
            "action": "資料不足",
            "action_class": "neutral",
        }

    # ── 7. 交易信號判定（對照交易中心策略門檻）──
    from sector_trader import DEFAULT_STRATEGIES, SECTOR_STOCKS
    from screener import get_symbol_sector as _get_sector

    # 找出該股票所屬的類股名稱
    sector_name = None
    for sname, stocks in SECTOR_STOCKS.items():
        if symbol in stocks or symbol.upper() in stocks:
            sector_name = sname
            break

    if sector_name and sector_name in DEFAULT_STRATEGIES:
        strat = DEFAULT_STRATEGIES[sector_name]
        tech = result.get("technical") or {}
        t_buy = tech.get("buy_score")
        t_sell = tech.get("sell_score")

        buy_threshold = strat["buy_threshold"]
        sell_threshold = strat["sell_threshold"]
        composite_threshold = 50  # 綜合分需 ≥ 50 才允許買入

        # ── 用引擎路徑計算五維綜合分數（與 auto_trader 一致）──
        from sector_auto_trader import fetch_signal_data, compute_signal, build_layers, compute_composite_score
        engine_composite = None
        engine_dims = []     # 五維拆解
        engine_layers = []   # layer 修正明細
        engine_direction = None
        engine_raw_buy = None
        engine_raw_sell = None
        try:
            _df = fetch_signal_data(symbol)
            if _df is not None:
                _layers = build_layers(strat)
                from sector_trader import SECTOR_IDS
                _sector_id_for_sig = SECTOR_IDS.get(sector_name, "")
                _sig = compute_signal(_df, strat["weights"], symbol,
                                      layers=_layers, sector_id=_sector_id_for_sig)
                if _sig:
                    engine_composite = compute_composite_score(symbol, _sig)
                    engine_direction = _sig.get("direction")
                    engine_raw_buy = _sig.get("raw_buy_score")
                    engine_raw_sell = _sig.get("raw_sell_score")

                    # 五維分數拆解
                    from screener import SECTOR_COMPOSITE_WEIGHTS
                    _sector_id = _get_sector(symbol)
                    _weights = SECTOR_COMPOSITE_WEIGHTS.get(_sector_id, SECTOR_COMPOSITE_WEIGHTS["default"])
                    _dim_scores = {}
                    _dim_scores["technical"] = _sig.get("raw_buy_score", _sig.get("buy_score", 50))

                    regime_scores_map = {
                        "強勢多頭": 90, "多頭": 75, "底部轉強": 70,
                        "盤整": 50, "高檔轉折": 25, "空頭": 15,
                    }
                    _dim_labels = {
                        "chipflow": "籌碼面", "technical": "技術面",
                        "fundamental": "基本面", "regime": "盤勢",
                        "sentiment": "消息面", "active_etf": "主動ETF",
                    }
                    for mod in _sig.get("layer_modifiers", []):
                        ln = mod.layer_name
                        if ln == "regime":
                            _dim_scores["regime"] = regime_scores_map.get(mod.regime, 50)
                        elif ln == "chipflow":
                            _dim_scores["chipflow"] = mod.details.get("buy_score", 50)
                        elif ln == "fundamental":
                            _dim_scores["fundamental"] = mod.details.get("buy_score", 50)
                        elif ln == "sentiment":
                            _dim_scores["sentiment"] = (
                                mod.details.get("buy_score")
                                if mod.details.get("buy_score") is not None else None
                            )

                        # Layer 修正明細
                        engine_layers.append({
                            "name": ln,
                            "label": _dim_labels.get(ln, ln),
                            "buy_mult": round(mod.buy_multiplier, 2),
                            "sell_mult": round(mod.sell_multiplier, 2),
                            "buy_offset": round(mod.buy_offset, 1),
                            "sell_offset": round(mod.sell_offset, 1),
                            "veto_buy": mod.veto_buy,
                            "reason": mod.reason or "",
                        })

                    for dim_key, dim_w in _weights.items():
                        s = _dim_scores.get(dim_key)
                        engine_dims.append({
                            "key": dim_key,
                            "label": _dim_labels.get(dim_key, dim_key),
                            "score": round(s, 1) if s is not None else None,
                            "weight": dim_w,
                        })
        except Exception as e:
            print(f"⚠️ 引擎信號計算失敗 {symbol}: {e}")

        comp = engine_composite if engine_composite is not None else (
            (result.get("recommendation") or {}).get("composite_score"))

        # 優先使用引擎的層調整後分數（與 auto_trader 邏輯完全一致）
        engine_buy_score = None
        engine_sell_score = None
        if engine_raw_buy is not None:
            try:
                engine_buy_score = round(float(_sig.get("buy_score", t_buy or 0)), 1)
                engine_sell_score = round(float(_sig.get("sell_score", t_sell or 0)), 1)
            except Exception:
                pass
        eff_buy = engine_buy_score if engine_buy_score is not None else t_buy
        eff_sell = engine_sell_score if engine_sell_score is not None else t_sell

        # 判定信號（與引擎邏輯一致：direction + confidence + composite）
        direction_ok = engine_direction == "BUY" if engine_direction else (
            tech.get("direction") == "BUY")
        buy_met = (eff_buy is not None and eff_buy >= buy_threshold
                   and direction_ok
                   and comp is not None and comp >= composite_threshold)
        sell_met = (eff_sell is not None and eff_sell >= sell_threshold)

        if buy_met:
            verdict = "符合買入條件"
            verdict_class = "buy"
        elif sell_met and (engine_direction == "SELL" if engine_direction else tech.get("direction") == "SELL"):
            verdict = "符合賣出條件"
            verdict_class = "sell"
        else:
            # 細分未達標原因（用 eff_buy/eff_sell 與引擎一致）
            reasons = []
            if eff_buy is not None and eff_buy >= buy_threshold and comp is not None and comp < composite_threshold:
                reasons.append(f"綜合分不足({comp:.0f}<{composite_threshold})")
            elif eff_buy is not None and eff_buy < buy_threshold:
                reasons.append(f"技術買分不足({eff_buy:.0f}<{buy_threshold})")
            if not direction_ok and engine_direction:
                reasons.append(f"方向非BUY({engine_direction})")
            verdict = "未達交易門檻：" + "、".join(reasons) if reasons else "未達交易門檻，觀望"
            verdict_class = "neutral"

        result["trading_signal"] = {
            "sector_name": sector_name,
            "strategy_name": strat["name"],
            "buy_threshold": buy_threshold,
            "sell_threshold": sell_threshold,
            "composite_threshold": composite_threshold,
            "stop_loss_pct": strat["stop_loss_pct"],
            "take_profit_pct": strat["take_profit_pct"],
            "tech_buy_score": eff_buy,
            "tech_sell_score": eff_sell,
            "raw_buy_score": round(engine_raw_buy, 1) if engine_raw_buy is not None else t_buy,
            "raw_sell_score": round(engine_raw_sell, 1) if engine_raw_sell is not None else t_sell,
            "composite_score": comp,
            "direction": engine_direction or tech.get("direction"),
            "verdict": verdict,
            "verdict_class": verdict_class,
            "dimensions": engine_dims,
            "layer_modifiers": engine_layers,
        }
    else:
        result["trading_signal"] = None

    # ── 8. 超選入榜查詢 ──
    from screener import get_screener_results
    screener_data = get_screener_results()
    screener_cats = screener_data.get("categories", [])
    screener_ranks = []
    for cat in screener_cats:
        stocks = cat.get("stocks", [])
        score_field = cat.get("score_field", "composite")
        for rank_idx, st in enumerate(stocks):
            if st.get("symbol") == symbol:
                # 解析 score_field dot-path 取得對應分數
                if score_field == "composite":
                    display_score = st.get("composite_score")
                else:
                    parts = score_field.split(".")
                    val = st
                    for p in parts:
                        val = val.get(p) if isinstance(val, dict) else None
                    display_score = val if isinstance(val, (int, float)) else st.get("composite_score")
                screener_ranks.append({
                    "id": cat["id"],
                    "name": cat["name"],
                    "icon": cat.get("icon", ""),
                    "rank": rank_idx + 1,
                    "total": len(stocks),
                    "score_label": cat.get("score_label", "綜合"),
                    "display_score": round(display_score, 1) if display_score is not None else None,
                    "composite_score": round(st.get("composite_score", 0)),
                })
                break
    result["screener_ranks"] = screener_ranks

    return result

# ============================================================
# 回測系統 API
# ============================================================
from backtest_engine import BacktestEngine
from indicators.registry import create_all_indicators, get_indicator_keys_map

_backtest_executor = ThreadPoolExecutor(max_workers=2)
_backtest_tasks: Dict[str, Dict] = {}
BACKTEST_DATA_DIR = Path(__file__).parent / "data" / "backtest"
BACKTEST_DATA_DIR.mkdir(exist_ok=True)
BACKTEST_INDEX_FILE = BACKTEST_DATA_DIR / "backtest_index.json"
BACKTEST_STATUS_FILE = BACKTEST_DATA_DIR / "task_status.json"


def _load_task_status() -> Dict:
    """從磁碟載入任務狀態（防止 server reload 丟失）"""
    if BACKTEST_STATUS_FILE.exists():
        try:
            with open(BACKTEST_STATUS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_task_status(task_id: str, status_data: dict):
    """將任務狀態持久化到磁碟"""
    all_status = _load_task_status()
    all_status[task_id] = status_data
    # 只保留最近 50 筆
    if len(all_status) > 50:
        sorted_keys = sorted(all_status.keys())
        for k in sorted_keys[:-50]:
            del all_status[k]
    with open(BACKTEST_STATUS_FILE, "w") as f:
        json.dump(all_status, f, ensure_ascii=False)

def fetch_ohlcv_for_backtest(symbol: str, period: str = "2y") -> pd.DataFrame:
    # Use yfinance for backtesting data
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval="1d")
    if df.empty:
        return None
    df.columns = [c.lower() for c in df.columns]
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df.index = pd.to_datetime(df.index.date)
    return df

def _execute_backtest(task_id: str, payload: dict):
    """在背景執行緒中執行回測（同步）"""
    try:
        symbol = payload["symbol"]
        period = payload.get("period", "2y")
        
        def progress_callback(progress, completed, total):
            update = {
                "progress": progress,
                "completed_combos": completed,
                "total_combos": total,
            }
            _backtest_tasks[task_id].update(update)

        engine = BacktestEngine(
            initial_capital=payload.get("initial_capital", 1_000_000),
            commission_rate=payload.get("commission_rate", 0.001425),
            tax_rate=payload.get("tax_rate", 0.003),
        )

        _backtest_tasks[task_id]['status'] = 'fetching_data'
        df = fetch_ohlcv_for_backtest(symbol, period)
        if df is None:
             raise ValueError(f"無法取得 {symbol} 的歷史資料")
        time.sleep(1) # 遵守 yfinance 速率限制

        _backtest_tasks[task_id]['status'] = 'preparing_signals'
        all_indicators = create_all_indicators(include_new=True)
        indicator_keys_map = get_indicator_keys_map(all_indicators)
        indicator_keys = list(indicator_keys_map.values())
        
        df_signals = engine.prepare_signals(df.copy(), all_indicators, indicator_keys_map)

        _backtest_tasks[task_id]['status'] = 'running_combos'
        results = engine.run_all_combos_with_progress(
            df_signals, indicator_keys,
            payload.get("min_combo_size", 2),
            payload.get("max_combo_size", 3), # Default to 3 for speed
            progress_callback
        )

        buy_hold_return = float((df.iloc[-1]['close'] / df.iloc[engine.warmup_period]['close'] - 1) * 100) if len(df) > engine.warmup_period else 0.0

        symbol_name = payload.get("symbol_name", "")
        if not symbol_name:
            symbol_name = fetch_stock_name(symbol) or ""

        result_data = {
            "symbol": symbol, "symbol_name": symbol_name, "period": period,
            "total_combos": len(results),
            "buy_and_hold_return": round(buy_hold_return, 2),
            "results": results,
        }
        _backtest_tasks[task_id].update({"status": "completed", "progress": 100})
        _save_backtest_result(task_id, result_data)
        _save_task_status(task_id, {"status": "completed", "progress": 100})

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        _backtest_tasks[task_id].update({"status": "failed", "error": str(e)})
        _save_task_status(task_id, {"status": "failed", "error": str(e)})

def _save_backtest_result(task_id: str, data: dict):
    # 儲存完整結果
    with open(BACKTEST_DATA_DIR / f"{task_id}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    # 更新索引檔
    index_data = {"records": []}
    if BACKTEST_INDEX_FILE.exists():
        with open(BACKTEST_INDEX_FILE, "r", encoding="utf-8") as f:
            try:
                index_data = json.load(f)
            except json.JSONDecodeError:
                pass

    best_result = data["results"][0] if data["results"] else {}
    index_data["records"].insert(0, {
        "task_id": task_id,
        "symbol": data["symbol"],
        "symbol_name": data.get("symbol_name", ""),
        "period": data["period"],
        "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "status": "completed",
        "best_combo": best_result.get("combo", []),
        "best_return": best_result.get("total_return_pct", 0),
        "buy_hold_return_pct": data["buy_and_hold_return"]
    })
    with open(BACKTEST_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=4)


@app.post("/api/backtest/run")
async def run_backtest(payload: dict):
    task_id = f"bt_{payload['symbol'].replace('.', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _backtest_tasks[task_id] = {"status": "queued", "progress": 0}
    
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_backtest_executor, _execute_backtest, task_id, payload)
    
    return {"status": "started", "task_id": task_id}

@app.get("/api/backtest/status/{task_id}")
async def get_backtest_status(task_id: str):
    # 優先從記憶體取（任務進行中）
    task = _backtest_tasks.get(task_id)
    if task:
        return {k: v for k, v in task.items() if k != "result"}
    # 記憶體沒有（server 可能 reload 過）→ 從磁碟讀
    disk_status = _load_task_status().get(task_id)
    if disk_status:
        return disk_status
    return {"status": "not_found"}

@app.get("/api/backtest/result/{task_id}")
async def get_backtest_result(task_id: str):
    # 直接從檔案讀取完整結果（不存記憶體，避免 JSON 序列化問題）
    result_file = BACKTEST_DATA_DIR / f"{task_id}.json"
    if result_file.exists():
        with open(result_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"status": "not_found_or_not_completed"}

@app.get("/api/backtest/history")
async def get_backtest_history():
    if not BACKTEST_INDEX_FILE.exists():
        return {"records": []}
    with open(BACKTEST_INDEX_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"records": []}

@app.delete("/api/backtest/history/{task_id}")
async def delete_backtest_record(task_id: str):
    """刪除單筆回測紀錄（索引 + 結果檔 + 狀態）"""
    # 刪除結果檔
    result_file = BACKTEST_DATA_DIR / f"{task_id}.json"
    if result_file.exists():
        result_file.unlink()

    # 從索引中移除
    if BACKTEST_INDEX_FILE.exists():
        try:
            with open(BACKTEST_INDEX_FILE, "r", encoding="utf-8") as f:
                index_data = json.load(f)
            index_data["records"] = [r for r in index_data.get("records", []) if r.get("task_id") != task_id]
            with open(BACKTEST_INDEX_FILE, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False, indent=4)
        except (json.JSONDecodeError, OSError):
            pass

    # 從狀態檔移除
    all_status = _load_task_status()
    if task_id in all_status:
        del all_status[task_id]
        with open(BACKTEST_STATUS_FILE, "w") as f:
            json.dump(all_status, f, ensure_ascii=False)

    # 從記憶體移除
    _backtest_tasks.pop(task_id, None)

    return {"status": "deleted", "task_id": task_id}


@app.get("/api/backtest/stats")
async def get_backtest_stats():
    """
    跨所有回測結果統計指標組合排行。
    掃描所有結果檔，對每個 combo 統計：
    - 在哪些股票拿到第 1 名
    - 平均報酬率、平均勝率
    - 出現在回測中的次數
    同一股票有多次回測時，只取最新一次（避免重複計算）。
    """
    from collections import defaultdict

    if not BACKTEST_INDEX_FILE.exists():
        return {"combos": []}

    try:
        with open(BACKTEST_INDEX_FILE, "r", encoding="utf-8") as f:
            index_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"combos": []}

    records = index_data.get("records", [])
    if not records:
        return {"combos": []}

    # 同一股票+期間只取最新一筆（index 已按時間倒序）
    seen = set()
    unique_tasks = []
    for r in records:
        key = f"{r['symbol']}_{r['period']}"
        if key not in seen:
            seen.add(key)
            unique_tasks.append(r)

    # combo_key → 統計資料
    combo_stats = defaultdict(lambda: {
        "first_place": [],       # 拿到第 1 名的 [{symbol, symbol_name, return}]
        "appearances": 0,        # 出現在幾次回測中
        "total_return": 0.0,
        "total_win_rate": 0.0,
        "total_sharpe": 0.0,
        "best_return": -999,
        "best_symbol": "",
        "best_symbol_name": "",
    })

    stock_count = 0
    for task_rec in unique_tasks:
        result_file = BACKTEST_DATA_DIR / f"{task_rec['task_id']}.json"
        if not result_file.exists():
            continue

        try:
            with open(result_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        results = data.get("results", [])
        if not results:
            continue

        symbol = data.get("symbol", "")
        symbol_name = data.get("symbol_name", "")
        stock_count += 1

        for i, r in enumerate(results[:50]):  # 只看前 50 名
            combo_key = "|".join(sorted(r.get("combo", [])))
            if not combo_key:
                continue
            s = combo_stats[combo_key]
            s["appearances"] += 1
            s["total_return"] += r.get("total_return_pct", 0)
            s["total_win_rate"] += r.get("win_rate", 0)
            s["total_sharpe"] += r.get("sharpe_ratio", 0)

            ret = r.get("total_return_pct", 0)
            if ret > s["best_return"]:
                s["best_return"] = ret
                s["best_symbol"] = symbol
                s["best_symbol_name"] = symbol_name

            if i == 0:  # 第 1 名
                s["first_place"].append({
                    "symbol": symbol,
                    "symbol_name": symbol_name,
                    "return_pct": r.get("total_return_pct", 0),
                })

            # 儲存 combo 顯示名稱（取一次就好）
            if "combo" not in s:
                s["combo"] = r.get("combo", [])
                s["combo_display"] = r.get("combo_display", r.get("combo", []))

    # 排序：先按 first_place 次數 desc，再按平均報酬 desc
    ranked = []
    for key, s in combo_stats.items():
        avg_ret = s["total_return"] / s["appearances"] if s["appearances"] else 0
        avg_wr = s["total_win_rate"] / s["appearances"] if s["appearances"] else 0
        avg_sharpe = s["total_sharpe"] / s["appearances"] if s["appearances"] else 0
        ranked.append({
            "combo": s.get("combo", key.split("|")),
            "combo_display": s.get("combo_display", key.split("|")),
            "first_count": len(s["first_place"]),
            "first_place": s["first_place"],
            "appearances": s["appearances"],
            "avg_return_pct": round(avg_ret, 2),
            "avg_win_rate": round(avg_wr, 1),
            "avg_sharpe": round(avg_sharpe, 2),
            "best_return_pct": round(s["best_return"], 2),
            "best_symbol": s["best_symbol"],
            "best_symbol_name": s["best_symbol_name"],
        })

    ranked.sort(key=lambda x: (x["first_count"], x["avg_return_pct"]), reverse=True)

    return {"combos": ranked[:30], "stock_count": stock_count}


# ── 超級選股系統 API ──

@app.get("/api/screener/picks")
async def get_screener_picks():
    """取得五大精選類別（從快取讀取）"""
    from screener import get_screener_results, trigger_background_scan, is_scanning

    data = get_screener_results()

    # 若無快取，自動觸發背景掃描
    if data.get("status") == "no_cache":
        if not is_scanning():
            trigger_background_scan()
        return {
            "categories": [],
            "updated_at": "",
            "total": 0,
            "scanning": True,
            "message": "首次掃描中，約需 1-2 分鐘...",
        }

    return {
        "categories": data.get("categories", []),
        "updated_at": data.get("updated_at", ""),
        "total": data.get("total", 0),
        "scanning": is_scanning(),
        "active_etfs": data.get("active_etfs", []),
    }


@app.get("/api/screener/full")
async def get_screener_full(min_score: float = 0, category: str = ""):
    """取得完整排行（可篩選）"""
    from screener import get_screener_results

    data = get_screener_results()
    results = data.get("results", [])

    # 篩選最低分數
    if min_score > 0:
        results = [r for r in results if r.get("composite", 0) >= min_score]

    # 篩選類別
    if category:
        categories = data.get("categories", [])
        cat_symbols = set()
        for cat in categories:
            if cat["id"] == category:
                cat_symbols = {s["symbol"] for s in cat.get("stocks", [])}
                break
        if cat_symbols:
            results = [r for r in results if r["symbol"] in cat_symbols]

    return {
        "results": results,
        "updated_at": data.get("updated_at", ""),
        "total": len(results),
    }


@app.post("/api/screener/refresh")
async def refresh_screener():
    """手動觸發背景重新掃描"""
    from screener import trigger_background_scan, is_scanning

    if is_scanning():
        return {"status": "already_scanning", "message": "掃描已在執行中"}

    started = trigger_background_scan()
    return {
        "status": "started" if started else "failed",
        "message": "背景掃描已啟動" if started else "啟動失敗",
    }


@app.post("/api/screener/clear-cache")
async def clear_screener_cache():
    """清除選股快取檔案"""
    from screener import clear_cache
    clear_cache()
    return {"status": "ok", "message": "快取已清除"}


@app.get("/api/active-etf-ranking")
async def get_active_etf_ranking():
    """取得主動式 ETF 持股排行（被領先大盤 ETF 重倉的台股）"""
    from layers.active_etf import get_active_etf_ranking as _get_ranking
    return _get_ranking()


@app.get("/api/custom-stocks")
async def list_custom_stocks():
    """取得使用者自選股清單"""
    from screener import get_custom_stocks
    return {"stocks": get_custom_stocks()}


@app.post("/api/custom-stocks")
async def add_custom_stock_api(symbol: str, name: str = ""):
    """新增自選股（搜尋時自動觸發）"""
    # 標準化代碼
    if not symbol.endswith(".TW"):
        symbol = symbol.split(".")[0] + ".TW"

    # 若沒提供名稱，自動查詢；查不到代表代號不存在，拒絕加入
    if not name:
        name = fetch_stock_name(symbol)
        if not name:
            return {"added": False, "reason": "not_found", "symbol": symbol}

    from screener import add_custom_stock, _BUILTIN_UNIVERSE
    if symbol in _BUILTIN_UNIVERSE:
        return {"added": False, "reason": "builtin", "symbol": symbol, "name": name}

    added = add_custom_stock(symbol, name)
    return {"added": added, "symbol": symbol, "name": name}


@app.delete("/api/custom-stocks")
async def remove_custom_stock_api(symbol: str):
    """移除自選股"""
    from screener import remove_custom_stock
    if not symbol.endswith(".TW"):
        symbol = symbol.split(".")[0] + ".TW"
    removed = remove_custom_stock(symbol)
    return {"removed": removed, "symbol": symbol}


@app.get("/api/sector-trading/{sector_id}/fundamental")
async def get_sector_fundamental(sector_id: str):
    """取得類股各標的基本面 P/E 分析"""
    mgr = get_manager(sector_id)
    if not mgr:
        return {"error": f"未知的類股 ID: {sector_id}"}

    from layers.fundamental import fetch_twse_pe_all, get_sector_pe_stats

    symbols = mgr.state.get("stocks", [])
    all_pe = fetch_twse_pe_all()

    if not all_pe:
        return {"sector_id": sector_id, "stocks": {}, "error": "無法取得 TWSE P/E 資料"}

    stats = get_sector_pe_stats(symbols, all_pe)

    # 補上股票中文名
    for sym in stats:
        if not stats[sym].get("name"):
            stats[sym]["name"] = mgr.stocks.get(sym, sym)

    return {"sector_id": sector_id, "stocks": _sanitize(stats)}


# ── 信號績效統計 API ──

@app.get("/api/signal-performance")
async def get_signal_performance(period: str = "6mo"):
    """取得信號績效統計結果（從快取讀取）。period: 6mo / 1y / 3y / 5y"""
    from signal_performance import get_performance_results, trigger_background_run, is_running

    data = get_performance_results(period)

    if data.get("status") == "no_cache":
        if not is_running(period):
            trigger_background_run(period)
        return {
            "status": "computing",
            "message": f"首次計算中（{period}），約需 3-5 分鐘...",
            "computing": True,
            "period": period,
        }

    data["computing"] = is_running(period)
    return data


@app.post("/api/signal-performance/refresh")
async def refresh_signal_performance(period: str = "6mo"):
    """手動觸發重新計算信號績效。period: 6mo / 1y / 3y / 5y"""
    from signal_performance import trigger_background_run, is_running

    if is_running(period):
        return {"status": "already_running", "message": f"計算已在執行中（{period}）"}

    started = trigger_background_run(period)
    return {
        "status": "started" if started else "failed",
        "message": f"背景計算已啟動（{period}），約需 3-5 分鐘" if started else "啟動失敗",
        "period": period,
    }


# ── 投資諮詢 API ──

from pydantic import BaseModel

class ConsultationRequest(BaseModel):
    symbol: str           # 股票代碼（如 "2317" 或 "2317.TW"）
    buy_price: float      # 買入均價（元）
    quantity: int         # 持有張數

@app.post("/api/consultation")
async def get_consultation(req: ConsultationRequest):
    """
    投資諮詢：根據持倉條件比對歷史類似盤勢/技術/籌碼情況，
    推算加碼 / 持有 / 減碼 / 出清建議
    """
    from consultation import consult_position
    try:
        result = consult_position(
            symbol=req.symbol,
            buy_price=req.buy_price,
            quantity=req.quantity,
        )
        return _sanitize(result)
    except Exception as e:
        logging.error(f"諮詢系統錯誤 {req.symbol}: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/api/screener/universe")
async def get_screener_universe():
    """回傳選股宇宙（供諮詢系統的股票搜尋）"""
    from screener import SCREENER_UNIVERSE
    return [{"symbol": k, "name": v} for k, v in SCREENER_UNIVERSE.items()]

from pydantic import BaseModel
class SettingsUpdate(BaseModel):
    telegram_chat_ids: str

class CustomStock(BaseModel):
    symbol: str = ""
    name: str = ""
    sector: str = ""

@app.get("/api/settings")
async def get_settings_api():
    from settings_manager import get_settings
    return get_settings()

@app.post("/api/settings")
async def update_settings_api(req: SettingsUpdate):
    from settings_manager import update_telegram_settings
    settings = update_telegram_settings(req.telegram_chat_ids)
    return {"status": "success", "settings": settings}

@app.post("/api/settings/stock")
async def add_custom_stock_api(req: CustomStock):
    from settings_manager import add_custom_stock
    from layers.fundamental import fetch_twse_pe_all

    sym = req.symbol.strip().upper()
    name = req.name.strip()

    if not sym and not name:
        return {"status": "error", "message": "請輸入股票代號或名稱"}

    # 1) 先查 TWSE 上市資料庫（涵蓋所有上市股，最權威）
    all_pe = fetch_twse_pe_all()
    code_only = sym.replace(".TW", "").replace(".TWO", "") if sym else ""

    matched_code = None
    matched_name = None

    if code_only and code_only in all_pe:
        matched_code = code_only
        matched_name = all_pe[code_only].get("name", "")
    elif name and not code_only:
        for c, info in all_pe.items():
            if info.get("name") == name:
                matched_code = c
                matched_name = info.get("name")
                break

    if matched_code:
        # 上市命中 → 統一掛 .TW，用 TWSE 官方名稱（避免使用者輸入錯字）
        final_sym = f"{matched_code}.TW"
        final_name = matched_name or name
    else:
        # 2) 上市找不到 → 嘗試上櫃（yfinance .TWO 驗證）
        if not code_only:
            return {"status": "error", "message": f"上市資料庫找不到「{name}」，請改輸入代號（如 2330）"}
        try:
            hist = yf.Ticker(f"{code_only}.TWO").history(period="1mo", interval="1d")
        except Exception as e:
            return {"status": "error", "message": f"驗證上櫃資料失敗：{e}"}
        if hist is None or hist.empty or len(hist) < 5:
            return {"status": "error", "message": f"找不到股票 {code_only}（上市/上櫃皆無資料），無法新增"}
        final_sym = f"{code_only}.TWO"
        final_name = name or f"上櫃-{code_only}"

    settings = add_custom_stock(final_sym, final_name, req.sector)

    # Update screener universe
    try:
        from screener import add_custom_stock as screener_add_stock
        screener_add_stock(final_sym, final_name)
    except Exception as e:
        print(f"Failed to add to screener: {e}")

    return {"status": "success", "symbol": final_sym, "name": final_name, "settings": settings}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
