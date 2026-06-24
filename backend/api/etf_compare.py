"""ETF vs 大盤 區間表現比較 API（漲跌雙向）+ 自訂 ETF 清單 CRUD。

- GET    /api/etf-compare?start=&end=&benchmark=   區間比較表
- GET    /api/etf-compare/watchlist                 使用者自訂清單
- POST   /api/etf-compare/watchlist?code=&name=     新增（yfinance 驗證）
- DELETE /api/etf-compare/watchlist?code=           移除
"""

from fastapi import APIRouter

router = APIRouter(tags=["etf-compare"])


@router.get("/api/etf-compare")
async def get_etf_compare(start: str = None, end: str = None, benchmark: str = "^TWII"):
    """ETF 比較池（9 檔 alpha + 自訂）在 [start,end] 的區間報酬 / 最大回撤 vs 大盤。"""
    from etf_compare import compare
    return compare(start=start, end=end, benchmark=benchmark)


@router.get("/api/etf-compare/watchlist")
async def list_watch_etfs():
    """使用者自訂的 ETF 比較清單。"""
    from settings_manager import get_watch_etfs
    return {"etfs": get_watch_etfs()}


@router.post("/api/etf-compare/watchlist", operation_id="etf_watchlist_add")
async def add_watch_etf_api(code: str, name: str = ""):
    """新增自訂 ETF（先用 yfinance 驗證代號有資料，查不到拒絕）。"""
    from etf_compare import validate_etf
    from settings_manager import add_watch_etf

    code_norm = code.strip().upper().replace(".TWO", "").replace(".TW", "")
    if not code_norm:
        return {"added": False, "reason": "empty_code"}
    ok, result = validate_etf(code_norm)
    if not ok:
        return {"added": False, "reason": result, "code": code_norm}
    final_name = name.strip() or result
    added = add_watch_etf(code_norm, final_name)
    return {"added": added, "code": code_norm, "name": final_name,
            "existed": not added}


@router.delete("/api/etf-compare/watchlist")
async def remove_watch_etf_api(code: str):
    """移除自訂 ETF。"""
    from settings_manager import remove_watch_etf
    code_norm = code.strip().upper().replace(".TWO", "").replace(".TW", "")
    removed = remove_watch_etf(code_norm)
    return {"removed": removed, "code": code_norm}
