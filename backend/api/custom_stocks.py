"""自選股 API（CRUD on /api/custom-stocks）。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。

依賴 main.py 內 fetch_stock_name（用於 POST 時補名稱）— lazy import 避循環依賴。
A5（stocks router）會把 fetch_stock_name 抽出來，到時這裡的 lazy 可改 module-top。
"""

from fastapi import APIRouter

router = APIRouter(tags=["custom-stocks"])


@router.get("/api/custom-stocks")
async def list_custom_stocks():
    """取得使用者自選股清單"""
    from screener import get_custom_stocks
    return {"stocks": get_custom_stocks()}


@router.post("/api/custom-stocks", operation_id="custom_stocks_add_quick")
async def add_custom_stock_quick(symbol: str, name: str = ""):
    """新增自選股（搜尋時自動觸發；query params）

    註：api/settings.py 內 /api/settings/stock 有對稱 handler `add_custom_stock_validated`，
    走 Pydantic body 介面、語意是「設定頁手動新增（含 TWSE/yfinance 驗證）」。
    """
    # 標準化代碼
    if not symbol.endswith(".TW"):
        symbol = symbol.split(".")[0] + ".TW"

    # 若沒提供名稱，自動查詢；查不到代表代號不存在，拒絕加入
    if not name:
        from main import fetch_stock_name
        name = fetch_stock_name(symbol)
        if not name:
            return {"added": False, "reason": "not_found", "symbol": symbol}

    from screener import add_custom_stock, is_builtin
    if is_builtin(symbol):
        return {"added": False, "reason": "builtin", "symbol": symbol, "name": name}

    added = add_custom_stock(symbol, name)
    return {"added": added, "symbol": symbol, "name": name}


@router.delete("/api/custom-stocks")
async def remove_custom_stock_api(symbol: str):
    """移除自選股"""
    from screener import remove_custom_stock
    if not symbol.endswith(".TW"):
        symbol = symbol.split(".")[0] + ".TW"
    removed = remove_custom_stock(symbol)
    return {"removed": removed, "symbol": symbol}
