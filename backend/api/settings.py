"""設定 API（含手動新增自選股的「設定頁」入口）。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。

註：本檔 add_custom_stock_validated 對應 api/custom_stocks.py 內 add_custom_stock_quick，
兩者走不同 path、不同入參、不同語意：
  - validated: /api/settings/stock + Pydantic body + TWSE/yfinance 驗證後存入
  - quick:     /api/custom-stocks + query params + 搜尋時自動觸發
"""

import yfinance as yf
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["settings"])


class SettingsUpdate(BaseModel):
    telegram_chat_ids: str


class CustomStock(BaseModel):
    symbol: str = ""
    name: str = ""
    sector: str = ""


@router.get("/api/settings")
async def get_settings_api():
    from settings_manager import get_settings
    return get_settings()


@router.post("/api/settings")
async def update_settings_api(req: SettingsUpdate):
    from settings_manager import update_telegram_settings
    settings = update_telegram_settings(req.telegram_chat_ids)
    return {"status": "success", "settings": settings}


@router.post("/api/settings/stock", operation_id="settings_add_custom_stock")
async def add_custom_stock_validated(req: CustomStock):
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
