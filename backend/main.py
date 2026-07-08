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
import hmac
import json
import logging
import re
from typing import List, Dict, Tuple
from datetime import datetime, timedelta
import time
import pandas as pd
import yfinance as yf
import ccxt.async_support as ccxt_async
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
import io
import urllib.request
import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# logging：開全域 INFO + 壓第三方雜訊（在此之前 info/debug 被吞，只能用 print 繞過）。
# 放在 sys.path.insert 後、domain 模組 import 前，確保各模組 runtime logging 都已設定好。
from logging_config import configure_logging
configure_logging()

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from signals.aggregator import SignalAggregator, MarketType
from business.sentiment import sentiment_engine
# ThreadPoolExecutor、Path、trading_manager 已搬到對應 router，A6b cleanup 移除 unused

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


# _sanitize 已抽至 api/_utils.py（A6b cleanup）— 給 /api/stock-analysis 等 endpoint 用
from api._utils import _sanitize  # noqa: F401


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

# ── 背景任務單例鎖 ──
# uvicorn --workers N 會 fork N 個 process，每個 process 都會 fire startup_event。
# 若不 gate，會跑出 N 份 auto_trader / daily_report_scheduler，造成：
#   - 10 份排程在 21:00 各送一則 daily report → 10 則重複訊息
#   - 10 份 auto_trader 競爭寫同一個 JSON 帳本 → race condition + 重複 Telegram
#   - 10 份 background_signal_updater → 10× 上游 API 呼叫
# 用 fcntl.flock 取 exclusive lock，process 退出時 kernel 自動釋放。
_SCHEDULER_LOCK_FILE = None

def _acquire_scheduler_lock() -> bool:
    """嘗試取得單例排程鎖。第一個 worker 拿到 → 跑背景任務；其他 worker → 只服務 HTTP。"""
    global _SCHEDULER_LOCK_FILE
    import fcntl
    lock_path = os.path.join(BACKEND_DIR, "data", "scheduler.lock")
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        f = open(lock_path, "w")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(f"pid={os.getpid()}\nacquired_at={datetime.now().isoformat()}\n")
        f.flush()
        _SCHEDULER_LOCK_FILE = f  # 保留 fd 到 process 結束，flock 才不會被釋放
        return True
    except (BlockingIOError, OSError):
        try:
            f.close()
        except Exception:
            pass
        return False


@app.on_event("startup")
async def startup_event():
    # ── 單例鎖：只有第一個 worker 跑背景任務，避免 --workers N 造成重複 ──
    if not _acquire_scheduler_lock():
        print(f"[Startup] PID {os.getpid()} 未取得 scheduler lock，本 worker 只服務 HTTP request（leader worker 在跑背景任務）")
        return

    print(f"[Startup] PID {os.getpid()} 取得 scheduler lock，啟動背景任務 + 排程")
    # 啟動背景更新任務
    asyncio.create_task(background_signal_updater())
    # 預載台股 ticker 資料（L2 本地 CSV → L3 TWSE，尊重 rate limit）
    asyncio.create_task(preload_tw_ticker_data())
    # 每日 14:35 自動刷新台股 ticker 資料（避免資料停滯）
    asyncio.create_task(daily_tw_data_refresh())
    # 每日 08:30 盤前資料健康檢查（K 線、基本面新鮮度 → Telegram）
    asyncio.create_task(premarket_data_health_check())
    # 每日 21:00 盤後摘要（心跳通知 + 今日下單/損益/連線健康）
    asyncio.create_task(daily_evening_summary())
    # 每日 18:00 法人 + 基本面 daily refresh（晚上能看到當天最新資料）
    asyncio.create_task(daily_institutional_refresh())
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


async def daily_institutional_refresh():
    """每交易日 18:00 抓當日三大法人 + 融資融券 + 基本面，讓晚上能看到當天最新資料。

    Why: TWSE 通常 17:00 後公佈當日法人；我們系統盤後 sleep 不會自動抓，
        所以隔日早上 09:00 第一個 cycle 才會 fetch（用昨日資料算訊號）。
        排這個 18:00 task 主動補抓，dashboard 晚上看到的就是當日最新。

    流程：
      1. 清快取時間戳（強制 fetch 不吃 cache）
      2. P/E + 營收（全市場一次）
      3. 三大法人（per-symbol，pacing 避免 FinMind rate limit）
      4. Telegram 報告抓了幾檔、成功幾筆
    """
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")

    while True:
        now = datetime.now(tw_tz)
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        print(f"[daily-inst-refresh] Next refresh in {int(wait_seconds/3600)}h {int((wait_seconds%3600)/60)}m")
        await asyncio.sleep(wait_seconds)

        today = datetime.now(tw_tz).date()
        if today.weekday() >= 5:
            print("[daily-inst-refresh] Weekend, skipping")
            continue

        # 2026-06-04：Telegram 發訊統一由 launchd /api/internal/trigger-daily-inst-refresh
        # 觸發（系統級 cron，wall-clock 對齊）。本 asyncio 迴圈拔掉發訊避免 drift 後重複。
        print("[daily-inst-refresh] Skipped (handled by launchd /api/internal/trigger-daily-inst-refresh)")


def _run_institutional_refresh():
    """同步版本，跑在 thread pool 內。回報 Telegram。"""
    from notifier import send_telegram

    report_lines = ["📊 <b>18:00 法人/基本面資料 daily refresh</b>"]
    universe = _collect_active_universe()
    target_syms = [s for s, m in universe if m != "futures"]

    # 1. 基本面 P/E + 營收（全市場一次）
    try:
        from layers.fundamental import (
            fetch_twse_pe_all, fetch_twse_revenue_all,
            _pe_cache, _rev_cache,
        )
        # 強制 fetch：把 cache time 設 0 讓 TTL 失效
        _pe_cache["time"] = 0
        _rev_cache["time"] = 0
        pe = fetch_twse_pe_all()
        rev = fetch_twse_revenue_all()
        report_lines.append(f"✅ 基本面 P/E：{len(pe)} 檔")
        report_lines.append(f"✅ 基本面 營收：{len(rev)} 檔")
    except Exception as e:
        report_lines.append(f"⚠️ 基本面 refresh 失敗：{e.__class__.__name__}")
        print(f"[daily-inst-refresh] fundamental: {e}")

    # 2. 三大法人（per-symbol，pacing）
    try:
        from layers.chipflow import (
            fetch_chip_summary,
            _chip_summary_cache, _inst_cache,
        )
        # cache 改 per-symbol 結構，直接清空整個 dict 等同於強制全部 refetch
        _chip_summary_cache.clear()
        _inst_cache.clear()

        success, fail = 0, 0
        for sym in target_syms:
            try:
                r = fetch_chip_summary(sym)
                if r:
                    success += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
            time.sleep(0.5)   # FinMind 寬鬆 pacing

        report_lines.append(f"✅ 三大法人：{success}/{success+fail} 檔成功（共 {len(target_syms)} 檔）")
        if fail > 0:
            report_lines.append(f"   ⚠️ {fail} 檔 fetch 失敗（可能 FinMind rate limit / 個股 OTC 沒資料）")
    except Exception as e:
        report_lines.append(f"⚠️ 法人 refresh 失敗：{e.__class__.__name__}")
        print(f"[daily-inst-refresh] chipflow: {e}")

    # 3. 推 Telegram
    try:
        send_telegram("\n".join(report_lines))
    except Exception as e:
        print(f"[daily-inst-refresh] telegram failed: {e}")
    print("[daily-inst-refresh] Done.")


async def premarket_data_health_check():
    """每個交易日 08:30（盤前 30 分鐘）檢查所有資料新鮮度，發 Telegram 報告。

    檢查項目：
      1. K 線（本地 CSV 最新日期）— 應該至少有昨日資料
      2. 基本面 P/E 與營收快取時間戳 — 14 天內為健康
      3. （法人 / 籌碼是 lazy fetch，靠 staleness guard 兜底，不在這邊查）

    結果以 Telegram 推給用戶，讓你開盤前知道哪些資料缺，可決定是否手動補。
    """
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")

    while True:
        now = datetime.now(tw_tz)
        target = now.replace(hour=8, minute=30, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        print(f"[premarket-check] Next check in {int(wait_seconds/3600)}h {int((wait_seconds%3600)/60)}m")
        await asyncio.sleep(wait_seconds)

        today = datetime.now(tw_tz).date()
        if today.weekday() >= 5:
            print("[premarket-check] Weekend, skipping")
            continue

        try:
            # 2026-06-03：Telegram 發訊改由 launchd /api/internal/trigger-premarket-check
            # 統一觸發（系統級 cron，wall-clock 對齊）。asyncio 這條保留「跑計算」即可，
            # 不再發訊；避免 asyncio drift 後又重複發第二則訊息。
            report = await asyncio.to_thread(_compute_data_freshness_report)
            print(f"[premarket-check] Done (no-telegram, by launchd). "
                  f"K-line stale: {len(report['stale_kline'])}, "
                  f"PE age: {report.get('pe_age_days')}, Rev age: {report.get('rev_age_days')}")
        except Exception as e:
            print(f"[premarket-check] error: {e}")


def _compute_data_freshness_report() -> dict:
    """掃所有 watch list 標的 + 全市場快取，回傳資料新鮮度摘要。"""
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")
    today = datetime.now(tw_tz).date()
    # 開盤前應該有「上個交易日」收盤；不是「昨天」（昨天可能是週六/週日）
    # Bug 2026-06-01：原本寫 yesterday，導致週一早上 86 檔誤判 stale。
    expected_latest_str = latest_closed_tw_trading_day()
    expected_latest = datetime.strptime(expected_latest_str, "%Y-%m-%d").date()

    report = {
        "stale_kline": [],     # [{symbol, last_date}]
        "missing_kline": [],   # [symbol] — 完全沒本地 CSV
        "pe_age_days": None,
        "rev_age_days": None,
        "total_checked": 0,
    }

    universe = _collect_active_universe()
    for sym, market in universe:
        if market == "futures":
            continue
        report["total_checked"] += 1
        local_df = load_local_history(sym)
        if local_df is None or len(local_df) == 0:
            report["missing_kline"].append(sym)
            continue
        try:
            last_idx = local_df.index[-1]
            last_date = last_idx.date() if hasattr(last_idx, "date") else pd.Timestamp(last_idx).date()
        except Exception:
            report["missing_kline"].append(sym)
            continue
        if last_date < expected_latest:
            age_days = (today - last_date).days
            report["stale_kline"].append({"symbol": sym, "last_date": str(last_date), "age_days": age_days})

    # 基本面快取年齡
    try:
        from layers.fundamental import _pe_cache, _rev_cache
        pe_ts = float(_pe_cache.get("fetched_at", 0) or 0)
        rev_ts = float(_rev_cache.get("fetched_at", 0) or 0)
        if pe_ts > 0:
            report["pe_age_days"] = round((time.time() - pe_ts) / 86400, 1)
        if rev_ts > 0:
            report["rev_age_days"] = round((time.time() - rev_ts) / 86400, 1)
    except Exception as e:
        print(f"[premarket-check] fundamental cache check error: {e}")

    return report


def _send_premarket_telegram(report: dict):
    """把 report 包成易讀的 Telegram 訊息推出去。"""
    from notifier import send_telegram

    lines = ["\U0001F305 <b>盤前資料健康檢查</b>"]
    lines.append(f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"檢查標的：{report['total_checked']} 檔")
    lines.append("")

    stale = report.get("stale_kline", [])
    missing = report.get("missing_kline", [])
    if not stale and not missing:
        lines.append("✅ K 線：全部到位（含昨日收盤）")
    else:
        total_bad = len(stale) + len(missing)
        lines.append(f"⚠️ K 線：{total_bad} 檔有問題")
        for x in stale[:5]:
            lines.append(f"   舊：{x['symbol']} 最新 {x['last_date']}（{x['age_days']} 天前）")
        if len(stale) > 5:
            lines.append(f"   ... 還有 {len(stale)-5} 檔過舊")
        for s in missing[:5]:
            lines.append(f"   缺：{s}（沒本地檔）")
        if len(missing) > 5:
            lines.append(f"   ... 還有 {len(missing)-5} 檔缺")

    pe_age = report.get("pe_age_days")
    if pe_age is None:
        lines.append("⚠️ 基本面 P/E：無資料")
    elif pe_age > 14:
        lines.append(f"⚠️ 基本面 P/E：{pe_age} 天舊（> 14 天，今日將不參與評分）")
    else:
        lines.append(f"✅ 基本面 P/E：{pe_age} 天")

    rev_age = report.get("rev_age_days")
    if rev_age is None:
        lines.append("⚠️ 基本面 營收：無資料")
    elif rev_age > 14:
        lines.append(f"⚠️ 基本面 營收：{rev_age} 天舊（> 14 天，今日將不參與評分）")
    else:
        lines.append(f"✅ 基本面 營收：{rev_age} 天")

    send_telegram("\n".join(lines))


async def daily_evening_summary():
    """每個交易日 21:00 推一份盤後摘要 Telegram，作為心跳通知。

    用戶不在國內時，每天 08:30 + 21:00 兩次 Telegram 都收得到 = 系統健康。
    收不到 = 出事了（service down / Telegram 壞 / 網路斷）。

    內容：
    - 服務 / quote source / 連線次數
    - 今日下單 / 成交 / 已實現損益 / 被擋筆數
    - 在飛訂單 / 進行中冷卻
    - kill-switch 是否觸發
    """
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")

    while True:
        now = datetime.now(tw_tz)
        target = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        print(f"[evening-summary] Next summary in {int(wait_seconds/3600)}h {int((wait_seconds%3600)/60)}m")
        await asyncio.sleep(wait_seconds)

        today = datetime.now(tw_tz).date()
        if today.weekday() >= 5:
            print("[evening-summary] Weekend, skipping")
            continue

        # 2026-06-03：Telegram 發訊統一由 launchd /api/internal/trigger-evening-summary
        # 觸發（系統級 cron，wall-clock 對齊）。本 asyncio 迴圈保留 as wake-on-schedule
        # placeholder，未來若想做其他盤後處理（無需發訊）可加在這。
        # 不再發訊；避免 asyncio drift 後又重複發第二則。
        print("[evening-summary] Skipped telegram (handled by launchd /api/internal/trigger-evening-summary)")


def _scan_account_last_trades(proj_root: str):
    """掃所有交易帳本，回傳 [(label, last_trade_dt), ...]。無紀錄者 last_trade_dt=None。

    Why: 2026-05-22 incident 之後用戶出國 9 天，所有帳本都沒成交，但「心跳」訊息
    發訊時間異常使用戶誤以為系統正常。改用「業務指標靜默」做最直接的健康檢測。

    雙階門檻設計：
      - 全帳本最近一筆 < 5 天    → 大標 🚨（事故型偵測）
      - 個別帳本 > 14 天       → 細節列表 🟡（漸進失效偵測，例如某 sector 靜默 2 個月）
    """
    from datetime import datetime as _dt
    paths = []
    # 舊「主帳戶」trading_account.json 已退場（2026-06-01）
    btc_acc = os.path.join(proj_root, "data", "btc_trading_account.json")
    if os.path.exists(btc_acc):
        paths.append(("BTC自動", btc_acc))
    sector_dir = os.path.join(proj_root, "data", "sector_accounts")
    if os.path.isdir(sector_dir):
        for fn in os.listdir(sector_dir):
            if fn.endswith("_account.json") and ".bak" not in fn:
                paths.append((fn.replace("_account.json", ""), os.path.join(sector_dir, fn)))

    results = []
    for label, p in paths:
        last_dt = None
        try:
            with open(p) as f:
                d = json.load(f)
            hist = d.get("history", [])
            if hist:
                # Bug 2026-06-02：原本用 hist[-1] 假設 list 是時間正序，但 sector_trader
                # 把新交易 insert(0) → list 是 newest-first。hist[-1] 拿到最舊的，
                # 結果 watchdog 永遠誤報所有帳本都靜默幾十天。
                # 改用 max(by time) — 不論 list 順序都拿到真正最新一筆。
                times = [t.get("time", "") for t in hist if t.get("time")]
                if times:
                    try:
                        last_dt = _dt.strptime(max(times)[:19], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        pass
        except Exception:
            pass
        results.append((label, last_dt))
    return results


def _send_evening_summary_telegram():
    """組裝並送出當日盤後摘要。"""
    import pytz
    from notifier import send_telegram

    tw_tz = pytz.timezone("Asia/Taipei")
    today = datetime.now(tw_tz).date()
    today_str = today.isoformat()
    proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    lines = [f"\U0001F319 <b>盤後摘要</b> {today_str}"]

    # ── 0. 業務靜默告警：兩階偵測（放最前面，preview 看得到）──
    GLOBAL_SILENT_DAYS = 5        # 全帳本最近一筆 ≥ 5 天 → 大標 🚨（事故型）
    PER_ACCOUNT_SILENT_DAYS = 14  # 個別帳本 ≥ 14 天 → 細節列表 🟡（漸進失效型）
    try:
        accounts = _scan_account_last_trades(proj_root)
        now_tw = datetime.now(tw_tz).replace(tzinfo=None)

        # 大標：找全帳本最近一筆，連 N 天無成交才告警
        non_null = [(label, t) for label, t in accounts if t is not None]
        if not non_null:
            lines.append("")
            lines.append("🚨 <b>所有帳本完全無交易紀錄</b> — 首次啟動或資料丟失？")
        else:
            latest_label, latest_dt = max(non_null, key=lambda x: x[1])
            global_silent_days = (now_tw - latest_dt).days
            if global_silent_days >= GLOBAL_SILENT_DAYS:
                lines.append("")
                lines.append(
                    f"🚨 <b>全帳本已連續 {global_silent_days} 天無任何成交</b>"
                    f"（最後 {latest_dt.strftime('%Y-%m-%d %H:%M')} {latest_label}）"
                )
                lines.append("   可能原因：broker reject / 信號全部未過門檻 / scheduler drift。")
                lines.append("   請檢查 logs/ 與 data/skipped_trades.jsonl")

        # 細節：列出靜默 ≥ 14 天的個別帳本
        stale_accounts = []
        for label, t in accounts:
            if t is None:
                stale_accounts.append((label, None, "無紀錄"))
                continue
            d = (now_tw - t).days
            if d >= PER_ACCOUNT_SILENT_DAYS:
                stale_accounts.append((label, d, t.strftime("%Y-%m-%d")))
        if stale_accounts:
            lines.append("")
            lines.append(f"🟡 個別帳本靜默 ≥ {PER_ACCOUNT_SILENT_DAYS} 天：")
            for label, days, when in stale_accounts:
                if days is None:
                    lines.append(f"   • {label}（無紀錄）")
                else:
                    lines.append(f"   • {label}（{days} 天，最後 {when}）")
    except Exception as e:
        lines.append(f"⚠️ 靜默檢測失敗：{e}")

    # ── 1. 服務 / quote source ──
    try:
        quote_src = os.environ.get("QUOTE_SOURCE", "yfinance")
        broker_mode = os.environ.get("BROKER_MODE", "virtual")
        lines.append(f"🟢 服務運行中 (PID {os.getpid()})")
        lines.append(f"   broker={broker_mode} / quote={quote_src}")
        # broker 真實狀態：BROKER_MODE 只是「意圖」，永豐 login 失敗會 silent fallback
        # 虛擬。這裡讀實際 broker 物件、把降級攤在盤後摘要裡（補 heartbeat 盲點）。
        try:
            from sector_auto_trader import auto_trader
            from brokers.factory import detect_broker_degradation
            setup = getattr(auto_trader, "_broker_setup", None)
            if setup is not None and getattr(setup, "brokers_by_sector", None):
                st = detect_broker_degradation(setup.brokers_by_sector)
                if st["mode"] == "sinopac":
                    if st["degraded"]:
                        lines.append(
                            f"   ⚠️ broker 降級虛擬：{', '.join(st['degraded'])}"
                            + (f"（仍走永豐：{', '.join(st['ok'])}）" if st["ok"] else "")
                        )
                        lines.append("   → 這些 sector 今日為紙上單、未送永豐")
                    elif st["ok"]:
                        lines.append(f"   ✅ 永豐 broker 正常：{', '.join(st['ok'])}")
        except Exception:
            pass
    except Exception:
        pass

    # ── 1b. 永豐 quote 連線健康度（當日異常 fallback 次數）──
    # 只在 quote=sinopac 時顯示；讓使用者一眼看到「永豐今天連線是否穩」，不用 grep log。
    try:
        if os.environ.get("QUOTE_SOURCE", "yfinance") == "sinopac":
            from quote_provider.sinopac_provider import get_fallback_stats
            fb = get_fallback_stats()
            if fb["total"] == 0:
                lines.append("   永豐報價：今日 0 次異常 fallback ✅")
            else:
                lines.append(
                    f"   ⚠️ 永豐報價今日 {fb['total']} 次異常 fallback yfinance"
                    f"（contract {fb['contract_not_found']}"
                    f" / kbars失敗 {fb['kbars_failed']}"
                    f" / kbars空 {fb['kbars_empty']}）"
                )
    except Exception:
        pass

    # ── 2. broker_state.json：今日下單 / 損益 / 在飛 / cooldown ──
    state_path = os.path.join(proj_root, "data", "broker_state.json")
    try:
        if os.path.exists(state_path):
            with open(state_path) as f:
                state = json.load(f)
            do = state.get("daily_orders", {}).get(today_str, {}) or {}
            total_orders = do.get("_total", 0) if isinstance(do, dict) else 0
            per_sector = {k: v for k, v in do.items() if k != "_total"} if isinstance(do, dict) else {}
            pnl = state.get("daily_realized_pnl", {}).get(today_str, {}) or {}
            total_pnl = sum(pnl.values()) if isinstance(pnl, dict) else 0
            pending = state.get("pending_orders", {}) or {}
            lock = state.get("daily_lock", {}).get(today_str)
            now_ts = time.time()
            active_cd = []
            for key, expires in (state.get("cooldowns", {}) or {}).items():
                try:
                    if float(expires) > now_ts:
                        active_cd.append(key)
                except (TypeError, ValueError):
                    continue

            lines.append("")
            lines.append(f"📊 今日下單：{total_orders} 筆")
            if per_sector:
                bits = [f"{k}={v}" for k, v in per_sector.items()]
                lines.append(f"   {' / '.join(bits)}")
            sign = "📈" if total_pnl >= 0 else "📉"
            lines.append(f"{sign} 已實現損益：{total_pnl:+,.0f} TWD")
            lines.append(f"⏳ 在飛訂單：{len(pending)} 筆")
            lines.append(f"⏱  進行中冷卻：{len(active_cd)} 筆")
            if lock:
                lines.append(f"🛑 KILL-SWITCH 觸發：{lock}")
    except Exception as e:
        lines.append(f"⚠️ broker_state 讀取失敗：{e}")

    # ── 3. 持倉摘要（sector_accounts）──
    try:
        sector_dir = os.path.join(proj_root, "data", "sector_accounts")
        total_positions = 0
        total_equity = 0.0
        if os.path.isdir(sector_dir):
            for fn in os.listdir(sector_dir):
                if not fn.endswith("_account.json") or ".bak" in fn:
                    continue
                try:
                    with open(os.path.join(sector_dir, fn)) as f:
                        acct = json.load(f)
                    holdings = acct.get("holdings", {}) or {}
                    total_positions += sum(1 for h in holdings.values() if (h.get("qty", 0) or 0) > 0)
                    total_equity += float(acct.get("balance", 0) or 0)
                    for h in holdings.values():
                        qty = h.get("qty", 0) or 0
                        avg = h.get("avg_price", 0) or 0
                        total_equity += qty * avg
                except Exception:
                    continue
        lines.append("")
        lines.append(f"💼 跨類股持倉：{total_positions} 檔，帳上總額 ≈ {int(total_equity):,} TWD")
    except Exception as e:
        lines.append(f"⚠️ 持倉統計失敗：{e}")

    # ── 4. 今日被擋筆數 ──
    try:
        skipped_path = os.path.join(proj_root, "data", "skipped_trades.jsonl")
        skipped_count = 0
        top_reasons = {}
        if os.path.exists(skipped_path):
            with open(skipped_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        if rec.get("time", "").startswith(today_str):
                            skipped_count += 1
                            r = (rec.get("reason") or "").split(":")[0]
                            top_reasons[r] = top_reasons.get(r, 0) + 1
                    except Exception:
                        continue
        lines.append(f"🚫 今日被擋：{skipped_count} 筆")
        if top_reasons:
            top3 = sorted(top_reasons.items(), key=lambda x: -x[1])[:3]
            lines.append("   " + " / ".join(f"{k}={v}" for k, v in top3))
    except Exception as e:
        lines.append(f"⚠️ skipped 統計失敗：{e}")

    # ── 5. Sinopac 連線健康度（從 log 數）──
    try:
        log_path = os.path.join(proj_root, "logs", "crypto-signal-pro.log")
        err_path = os.path.join(proj_root, "logs", "crypto-signal-pro-error.log")
        session_up = 0
        retry_fail = 0
        for p in (log_path, err_path):
            if os.path.exists(p):
                with open(p, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "Event: Session up" in line:
                            session_up += 1
                        elif "after" in line and "attempts" in line and "place_order" in line:
                            retry_fail += 1
        lines.append("")
        lines.append(f"🔌 Sinopac Session up：{session_up}（健康 1-4，> 10 表抖動）")
        if retry_fail > 0:
            lines.append(f"⚠️ retry 後仍失敗：{retry_fail} 筆 — 請檢查永豐主機")
    except Exception:
        pass

    lines.append("")
    lines.append("<i>明早 08:30 會再送一次盤前摘要</i>")
    send_telegram("\n".join(lines))


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


# /api/futures-info 已搬至 api/stocks.py


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

    market_open = is_tw_market_open()
    remaining = tw_seconds_until_next() if market_open else None

    # ── 新鮮度驗證（P0 fix 2026-06-03）──
    # Bug：過去直接用 df.iloc[-2:] 算漲跌，若 CSV 已舊則 df[-1]/df[-2] 可能是更早前的
    # 兩根 K（如「上週四 vs 上週五」），算出來的 % 看似合理但實際是「跨週」誤導值。
    # 用戶 6/3 12:10 看到 南亞科 410(+7.10%) 但真實 406 → 基準 382.82 是 5/29 舊 CSV 殘留。
    #
    # 修法：驗證 df 最新日期 ≥「上個交易日」；若否 → stale，不算 change_24h（=0）+
    # data_source 加 _stale 後綴讓前端可標警告。盤中當日 partial snapshot 不算 stale。
    stale_data = False
    try:
        last_idx = df.index[-1]
        last_date = (last_idx.date() if hasattr(last_idx, 'date')
                     else datetime.strptime(str(last_idx)[:10], "%Y-%m-%d").date())
        expected_latest = datetime.strptime(latest_closed_tw_trading_day(), "%Y-%m-%d").date()
        today_tw = datetime.now(pytz.timezone('Asia/Taipei')).date()
        # 允許「今日盤中 partial」：last_date == today 即使 > expected_latest 也 OK
        if last_date < expected_latest and last_date != today_tw:
            stale_data = True
            print(f"[stale-data] {symbol} df last={last_date} < expected={expected_latest}, "
                  f"change_24h 強制歸 0 避免錯誤百分比 (source={data_source})")
    except Exception as e:
        print(f"[stale-data] {symbol} 日期判斷失敗：{e}")

    tw_change = 0.0
    if not stale_data and len(df) >= 2:
        prev_c = float(df['close'].iloc[-2])
        curr_c = float(df['close'].iloc[-1])
        if prev_c > 0:
            tw_change = round((curr_c - prev_c) / prev_c * 100, 2)

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
                "stale":      stale_data,
            }
        },
        "data_source": data_source + ("_stale" if stale_data else ""),
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
    # data_kind: 盤中當日的 partial snapshot 標 "intraday"，其他（盤後 EOD、歷史日）標 "eod"
    # 用途：盤後讀取時拒絕 intraday cache，避免盤中 snapshot 蓋過正式收盤價
    today_tw_str = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%Y-%m-%d")
    data_kind = "intraday" if (market_open and data_date == today_tw_str) else "eod"
    signals_cache[f"signals_{symbol}"] = {
        "data": result_data,
        "fetched_at": time.time(),
        "data_date": data_date,
        "data_kind": data_kind,
    }
    # 記錄台股更新時間
    last_update_timestamps["tw_stock"] = datetime.now().strftime("%H:%M:%S")
    return result_data


def _fetch_yfinance_df(symbol: str):
    """抓取台股日線 DataFrame（透過 quote_provider 抽象；預設 yfinance，可切 sinopac）。
    函式名保留 yfinance 為歷史包袱，實際 source 由 QUOTE_SOURCE env 決定。
    回傳格式與 fetch_twse_daily 相容（index=date, columns=open/high/low/close/volume）。
    """
    from quote_provider import get_quote_provider
    provider = get_quote_provider()
    base = symbol.split('.')[0] if '.' in symbol else symbol
    for suffix in ['.TW', '.TWO']:
        yf_sym = base + suffix
        try:
            df = provider.get_history(yf_sym, period_days=365, interval='1d')
            if df is None or df.empty or len(df) < 30:
                continue
            df = df[['open', 'high', 'low', 'close', 'volume']].copy()
            df.index.name = 'date'
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            print(f"[quote:{provider.name}-df] {yf_sym}: {len(df)} rows")
            return df
        except Exception as e:
            print(f"[quote:{provider.name}-df] {yf_sym} failed: {e}")
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
        cached_kind = cached.get("data_kind", "eod")  # 舊資料無此欄位視為 eod
        latest_trading_day = latest_closed_tw_trading_day()
        today_tw_str = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%Y-%m-%d")
        # 盤中：必須是今日 intraday 且 60 秒內
        # 盤後：必須是 eod 且資料日期 == 最近收盤日
        #   → 盤中存的 intraday 在盤後一律作廢，避免 partial snapshot 蓋過真正收盤價
        if market_open:
            is_fresh = (cached_kind == "intraday"
                        and cached_data_date == today_tw_str
                        and age < TW_RATE_LIMIT_SEC)
        else:
            is_fresh = (cached_kind == "eod"
                        and cached_data_date == latest_trading_day)
        if is_fresh:
            print(f"[signals L1] {symbol} kind={cached_kind} age={int(age)}s data_date={cached_data_date}")
            result = dict(cached["data"])
            result["next_update_in"] = remaining
            result["data_source"] = cached["data"]["data_source"] + ("" if market_open else "_closed")
            return result
        else:
            print(f"[signals L1 stale] {symbol} kind={cached_kind} open={market_open} data_date={cached_data_date} latest={latest_trading_day}, refetch")

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


# /api/update-status 已搬至 api/stocks.py


# /api/stock-info 已搬至 api/stocks.py


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
    """判斷台灣市場目前是否在『可抓即時行情』時段 (週一至五 09:00 - 14:15)。

    給 1m K bar / 即時報價 cache 用，含收盤後 45 分緩衝。
    交易決策的時段判斷請用 backend.brokers.market_hours.is_orderable_now()。
    """
    from brokers import market_hours as _mh
    return _mh.is_data_capture_window()


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


# ── launchd 觸發用 internal endpoints（與 asyncio 內排程「並存」雙保險） ──
# Why: asyncio.sleep 在 macOS 睡眠後 wall-clock drift（2026-05 incident 觀察 24h 排程
# 累積 1-2h 誤差）。改用 launchd 系統 timer 從外部 POST 觸發，事件對齊 wall-clock。
# 詳見 scripts/launchd/local.crypto-*-trigger.plist。

_internal_key_warned = False


async def require_internal_key(x_internal_key: str = Header(default="")):
    """/api/internal/* 的共用 auth：驗 X-Internal-Key header。

    Why: 這些 endpoint 會觸發 Telegram 發訊 / 法人 refresh，而服務 bind 0.0.0.0:8000，
    LAN 上任何裝置都能 POST 觸發（甚至灌假摘要）。加共享金鑰擋住外部誤觸。

    相容性（避免改 A 壞 B）：未設定 CSP_INTERNAL_KEY 時 fail-open（僅 log 一次 warning），
    確保部署新 code 不會在使用者於 .env 設好金鑰前，就把每日 08:30/18:00/21:00 排程打掛。
    在 .env 設好金鑰並重啟 service 後即自動轉為強制驗證。
    """
    expected = os.environ.get("CSP_INTERNAL_KEY", "").strip()
    if not expected:
        global _internal_key_warned
        if not _internal_key_warned:
            print("[internal-auth] CSP_INTERNAL_KEY 未設定，/api/internal/* 暫不驗證（fail-open）")
            _internal_key_warned = True
        return
    # 用 bytes 比對：str 版 compare_digest 僅限 ASCII，非 ASCII header 會丟 TypeError → 500。
    if not hmac.compare_digest(x_internal_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="invalid or missing internal key")


@app.post("/api/internal/trigger-premarket-check", dependencies=[Depends(require_internal_key)])
async def trigger_premarket_check():
    """供 launchd 在 08:30 (台北) 觸發。週末/假日由 endpoint 內部自行跳過。"""
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tw_tz)
    if now.weekday() >= 5:
        return {"skipped": "weekend", "now": now.isoformat()}
    try:
        # 順手暖機處置股清單（08:30 在 reserve_stock 服務時段 08:00-14:30 內）
        await asyncio.to_thread(_warm_disposition_cache)
        # TDCC 大戶持股 weekly 抓取（內部 dedupe、同週不重抓）
        await asyncio.to_thread(_warm_large_holder_cache)
        report = await asyncio.to_thread(_compute_data_freshness_report)
        await asyncio.to_thread(_send_premarket_telegram, report)
        return {"status": "ok", "triggered_by": "launchd", "now": now.isoformat()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _warm_disposition_cache():
    """提早拉處置股清單，避免 09:00 開盤後第一筆 SELL 才 lazy load 卡住。

    若 broker 還沒注入或不是 sinopac broker，安靜略過。
    若持倉中含處置股，順帶發一封 telegram 摘要（每日只一次）。
    """
    try:
        from brokers.disposition_guard import get_guard
        guard = get_guard()
        if guard is None:
            return
        # 用 sector trader 任一 manager 的 broker.api（共用同個 Shioaji instance）
        from sector_trader import get_all_managers
        mgrs = get_all_managers()
        sinopac_api = None
        for m in mgrs.values():
            broker = getattr(m, "_broker", None)
            if broker is not None and hasattr(broker, "api"):
                sinopac_api = broker.api
                break
        if sinopac_api is None:
            print("[disposition-warmup] no sinopac broker yet, skip")
            return

        codes = guard.get_disposition_set(sinopac_api)
        snap = guard.snapshot()
        print(f"[disposition-warmup] punish() ok={snap['ok']} date={snap['date']} count={snap['count']}")

        # 檢查持倉中是否有處置股，有的話發 telegram 摘要（去重一日 1 次）
        if codes and guard.should_send_daily_telegram():
            disposed_held = []
            for sector_id, m in mgrs.items():
                # snapshot holdings 後再迭代，避免 sector_auto_trader 同時改 state["holdings"]
                # 觸發 RuntimeError: dictionary changed size during iteration
                holdings_snap = dict(m.state.get("holdings", {}))
                for sym, h in holdings_snap.items():
                    if h.get("qty", 0) <= 0:
                        continue
                    code = sym.split(".")[0]
                    if code in codes:
                        disposed_held.append((sector_id, sym, h.get("qty"), h.get("avg_price")))
            if disposed_held:
                from notifier import send_telegram
                lines = ["⚠️ <b>持倉含處置股</b>"]
                for sector_id, sym, qty, avg in disposed_held:
                    lines.append(f"  [{sector_id}] {sym} {qty} 股 @{avg}")
                lines.append("")
                lines.append("處置股 SELL 在 sim 必失敗（reserve_stock 是 no-op）；")
                lines.append("prod 真錢時系統會自動走預收券流程。")
                lines.append("處置期結束後自動恢復正常。")
                send_telegram("\n".join(lines))
                print(f"[disposition-warmup] telegram sent: {len(disposed_held)} disposed holdings")
    except Exception as e:
        # 暖機失敗不擋 premarket 主流程；但要印出 traceback 否則 silent fail 找不到原因
        import traceback
        print(f"[disposition-warmup] error: {e!r}\n{traceback.format_exc()}")


def _warm_large_holder_cache():
    """TDCC 大戶持股 weekly 抓取（cache 內部 dedupe、同週不重抓）。

    由 trigger_premarket_check 在 _warm_disposition_cache 之後呼叫。
    純揭露用、不影響交易；失敗不擋主流程。
    """
    try:
        from layers.large_holder import get_cache as get_large_holder_cache
        cache = get_large_holder_cache()
        updated = cache.fetch()
        meta = cache.snapshot_meta()
        print(f"[large-holder-warmup] updated={updated} fetch_date={meta['fetch_date']} "
              f"symbols={meta['symbol_count']}")
    except Exception as e:
        import traceback
        print(f"[large-holder-warmup] error: {e!r}\n{traceback.format_exc()}")


@app.post("/api/internal/trigger-evening-summary", dependencies=[Depends(require_internal_key)])
async def trigger_evening_summary():
    """供 launchd 在 21:00 (台北) 觸發。週末/假日不跳過——盤後摘要含「N 日無交易」告警，
    任何時候都該發。"""
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")
    try:
        await asyncio.to_thread(_send_evening_summary_telegram)
        return {"status": "ok", "triggered_by": "launchd", "now": datetime.now(tw_tz).isoformat()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/internal/net-recovered", dependencies=[Depends(require_internal_key)])
async def net_recovered(down_minutes: int = 0):
    """網路 watchdog 偵測到斷網、自動重連 wifi 成功後，由它呼叫本端點發 Telegram。

    Telegram token 留在 app 的 .env、watchdog 腳本完全不碰密鑰（只帶內部 key 打 localhost）。
    訊息為固定字串、不含任何 PII。
    """
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tw_tz)
    try:
        from notifier import send_telegram
        gap = f"（中斷約 {down_minutes} 分）" if down_minutes else ""
        msg = (f"🔌 <b>網路自動復原</b>\n"
               f"偵測到對外網路中斷{gap}，已自動重連 Wi-Fi 並恢復連線。\n"
               f"時間：{now.strftime('%Y-%m-%d %H:%M')}")
        ok = await asyncio.to_thread(send_telegram, msg)
        return {"status": "ok" if ok else "telegram_failed", "now": now.isoformat()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/internal/trigger-ai-news", dependencies=[Depends(require_internal_key)])
async def trigger_ai_news():
    """供 launchd 觸發 AI 新聞摘要（抓公開源 → 去重 → Gemini 摘要(可缺席) → Telegram）。

    無新項目時不發送（避免空訊息）。只處理公開新聞、不接觸交易資料。
    """
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")
    try:
        from ai_news import run_digest
        stats = await asyncio.to_thread(run_digest)
        return {"status": "ok", **stats, "now": datetime.now(tw_tz).isoformat()}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


@app.post("/api/internal/trigger-disposition-alert", dependencies=[Depends(require_internal_key)])
async def trigger_disposition_alert():
    """供 launchd 盤後觸發：推播「今日新進『再1次就處置』」名單（純觀察、非投資建議）。

    seen store 去重（7 天內不重推）；無新進 / 資料異常時不發送。不接觸交易、不下單。
    """
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")
    try:
        from disposition_radar import run_disposition_alert
        stats = await asyncio.to_thread(run_disposition_alert)
        return {"status": "ok", **stats, "now": datetime.now(tw_tz).isoformat()}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


@app.post("/api/internal/trigger-daily-inst-refresh", dependencies=[Depends(require_internal_key)])
async def trigger_daily_inst_refresh():
    """供 launchd 在 18:00 (台北) 觸發。每交易日 TWSE 公佈當日法人後抓資料 + 發 Telegram 報告。
    週末/假日由 endpoint 內部 _run_institutional_refresh 自行 skip。"""
    import pytz
    tw_tz = pytz.timezone("Asia/Taipei")
    now = datetime.now(tw_tz)
    if now.weekday() >= 5:
        return {"skipped": "weekend", "now": now.isoformat()}
    try:
        await asyncio.to_thread(_run_institutional_refresh)
        return {"status": "ok", "triggered_by": "launchd", "now": now.isoformat()}
    except Exception as e:
        return {"status": "error", "error": str(e)}

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


def _daily_cache_is_fresh(cached: dict) -> bool:
    """1d 快取的最後一根 K 線日期 >= 最近一個已收盤交易日才算新鮮。

    cache_age 不是判斷依據（盤後一根新 K 也不會出現）；唯一指標是 cache 末根日期。
    末根 candle 為 None / 缺 time / 不在預期日 → False，呼叫端 fall-through 重讀 CSV。

    時區處理：candle time 兩種來源編碼不同——
    CSV / TWSE 走 naive pd.Timestamp（pandas 把它當 UTC，∴ 1780358400 = 2026-06-02 UTC 0:00）；
    yfinance 走 tz-aware Asia/Taipei midnight（1780329600 = 2026-06-01 UTC 16:00 = 同一日 Taipei 0:00）。
    在 Taipei tz 讀回後兩者都會落在正確的交易日。
    """
    candles = cached.get("candles") or []
    if not candles:
        return False
    last_ts = candles[-1].get("time")
    if last_ts is None:
        return False
    last_date = datetime.fromtimestamp(int(last_ts), tz=pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d")
    return last_date >= latest_closed_tw_trading_day()


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

        # 有過期快取則回傳，避免空白（此處刻意不卡 freshness：上游全失敗時舊資料勝過空白）
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

    # B1: 有快取 → 直接回傳（1d 必須末根 K 為最近一個已收盤交易日，否則 fall-through 重讀 CSV）
    if cached and (timeframe != "1d" or _daily_cache_is_fresh(cached)):
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

# /api/chart 已搬至 api/stocks.py

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
# 類股虛擬交易 API — 已搬至 api/sector_trading.py
# 註：/api/symbol-sector 是 symbol lookup helper，與 sector-trading 路徑無關，留在本檔
# ============================================================

from api.sector_trading import router as sector_trading_router
app.include_router(sector_trading_router)

# /api/symbol-sector 已搬至 api/stocks.py

# ════════════════════════════════════════════════════
# BTC 自動交易 API — 已搬至 api/btc_trading.py
# 通知 / 每日報告 API — 已搬至 api/notifications.py
# ════════════════════════════════════════════════════

from api.btc_trading import router as btc_trading_router
from api.notifications import router as notifications_router
app.include_router(btc_trading_router)
app.include_router(notifications_router)


# /api/stock-lookup 已搬至 api/stocks.py


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



# ── 超級選股系統 API — 已搬至 api/screener.py 與 api/custom_stocks.py ──
# ── 台股 / 標的查詢 API（低耦合 endpoints）— 已搬至 api/stocks.py ──

from api.screener import router as screener_router
from api.custom_stocks import router as custom_stocks_router
from api.stocks import router as stocks_router
from api.active_etf import router as active_etf_router
from api.etf_compare import router as etf_compare_router
from api.chip_disclosure import router as chip_disclosure_router
from api.disposition_radar import router as disposition_radar_router
app.include_router(screener_router)
app.include_router(custom_stocks_router)
app.include_router(stocks_router)
app.include_router(active_etf_router)
app.include_router(etf_compare_router)
app.include_router(chip_disclosure_router)
app.include_router(disposition_radar_router)


# 註：/api/sector-trading/{sector_id}/fundamental 已搬至 api/sector_trading.py
# 註：/api/screener/universe 已搬至 api/screener.py（順手收編孤兒位置）


# ── A6: 回測 / 績效 / 諮詢 / 設定 router 已搬至 api/ 子目錄 ──

from api.backtest import router as backtest_router
from api.performance import router as performance_router
from api.consultation import router as consultation_router
from api.settings import router as settings_router
app.include_router(backtest_router)
app.include_router(performance_router)
app.include_router(consultation_router)
app.include_router(settings_router)






if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
