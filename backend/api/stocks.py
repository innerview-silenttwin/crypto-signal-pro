"""台股 / 標的查詢 API（A5a + A5b：低-中耦合 endpoints）。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。

本檔放與 main.py 內部 state/helper 耦合度可控的 endpoint：
- /api/futures-info：期貨名稱對照
- /api/update-status：更新時間戳
- /api/symbol-sector：symbol → sector 查詢
- /api/stock-lookup：模糊搜尋
- /api/stock-info：股票名稱查詢
- /api/chart：K 線資料

高耦合 endpoint（/api/tw-signals、/api/ticker-summary、/api/stock-analysis）尚未搬，
留 main.py 等之後抽 helper / state module 後再處理。
"""

import ccxt.async_support as ccxt_async

from fastapi import APIRouter

router = APIRouter(tags=["stocks"])


@router.get("/api/futures-info")
async def get_futures_info(symbol: str):
    """回傳台股期貨名稱對照。"""
    from main import FUTURES_NAMES
    sym_key = symbol.upper().split('.')[0]
    name = FUTURES_NAMES.get(sym_key, '')
    return {"symbol": symbol, "name": name}


@router.get("/api/update-status")
async def get_update_status():
    """回傳 crypto / tw 各自的最新更新時間。"""
    from main import last_update_timestamps, is_tw_market_open, tw_seconds_until_next
    return {
        "crypto_updated_at": last_update_timestamps["crypto"],
        "tw_updated_at": last_update_timestamps["tw_stock"],
        "tw_market_open": is_tw_market_open(),
        "tw_next_fetch_in": tw_seconds_until_next() if is_tw_market_open() else None,
    }


@router.get("/api/symbol-sector")
async def get_symbol_sector_endpoint(symbol: str):
    """查詢股票所屬產業 ID（給前端做 symbol→tab 自動切換用）"""
    from screener import get_symbol_sector
    # normalize: 純數字 → 加 .TW
    sym = symbol.strip().upper()
    if sym.isdigit():
        sym = f"{sym}.TW"
    sec = get_symbol_sector(sym)
    return {"symbol": sym, "sector_id": sec if sec != "default" else None}


@router.get("/api/stock-lookup")
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


# ── A5b：中-低耦合 endpoints ─────────────────────────────────────────

@router.get("/api/stock-info")
async def get_stock_info(symbol: str):
    """提供簡易股票名稱查詢，用於前端顯示。"""
    from main import fetch_stock_name
    name = fetch_stock_name(symbol)
    return {"symbol": symbol, "name": name or ""}


@router.get("/api/chart")
async def get_chart_data(symbol: str = "BTC/USDT", timeframe: str = "1d", market: str = "crypto"):
    """K 線資料：crypto 走 ccxt、台股/期貨走 get_tw_chart_data。"""
    import asyncio
    from main import get_tw_chart_data, fetch_ohlcv_async

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
