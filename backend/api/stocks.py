"""台股 / 標的查詢 API（A5a：低耦合 endpoints）。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。

本檔只放與 main.py 內部 state/helper 耦合度低的 endpoint：
- /api/futures-info：期貨名稱對照
- /api/update-status：更新時間戳
- /api/symbol-sector：symbol → sector 查詢
- /api/stock-lookup：模糊搜尋

高耦合 endpoint（/api/tw-signals、/api/stock-info、/api/stock-analysis）尚未搬，
留 main.py 等之後抽 helper / state module 後再處理。
"""

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
