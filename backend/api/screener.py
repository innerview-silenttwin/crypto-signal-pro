"""選股系統 API。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。

收編位置：
- /api/screener/{picks,full,refresh,clear-cache,universe} (原 main.py 多處)

註：/api/active-etf-ranking 已拆到 api/active_etf.py（A4 cleanup），
本 router 才能用乾淨的 prefix="/api/screener"。
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/screener", tags=["screener"])


@router.get("/picks")
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
        "etf_diff_prev_date": data.get("etf_diff_prev_date"),
    }


@router.get("/full")
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


@router.post("/refresh")
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


@router.post("/clear-cache")
async def clear_screener_cache():
    """清除選股快取檔案"""
    from screener import clear_cache
    clear_cache()
    return {"status": "ok", "message": "快取已清除"}


@router.get("/universe")
async def get_screener_universe():
    """回傳選股宇宙（供諮詢系統的股票搜尋）"""
    from screener import SCREENER_UNIVERSE
    return [{"symbol": k, "name": v} for k, v in SCREENER_UNIVERSE.items()]
