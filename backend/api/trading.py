"""主帳戶虛擬交易 API。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。
依賴：trading_manager (singleton) + current_signals (main.py 全域 dict，lazy import 避循環)。
"""

from fastapi import APIRouter

from trading_manager import trading_manager

router = APIRouter(prefix="/api/trading", tags=["trading"])


@router.post("/toggle")
async def toggle_trading(active: bool = False):
    """啟動/停止自動交易"""
    is_active = trading_manager.toggle_active(active)
    return {"is_active": is_active}


@router.get("/status")
async def get_trading_status():
    """取得帳戶摘要（資產淨值、持倉、損益）"""
    # current_signals 住在 main.py，lazy import 避免循環依賴
    from main import current_signals

    current_prices = {}
    for symbol, data in current_signals.items():
        sigs = data.get("signals", {})
        if "1d" in sigs:
            current_prices[symbol] = sigs["1d"].get("price", 0)
    return trading_manager.get_summary(current_prices)


@router.get("/history")
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


@router.get("/symbols")
async def get_watchlist_symbols():
    """取得監控標的清單"""
    return trading_manager.state.get("symbols", [])


@router.post("/symbols/add")
async def add_watchlist_symbol(symbol: str):
    """新增監控標的"""
    success = trading_manager.add_symbol(symbol)
    return {"success": success, "symbols": trading_manager.state.get("symbols", [])}


@router.post("/symbols/remove")
async def remove_watchlist_symbol(symbol: str):
    """移除監控標的"""
    success = trading_manager.remove_symbol(symbol)
    return {"success": success, "symbols": trading_manager.state.get("symbols", [])}
