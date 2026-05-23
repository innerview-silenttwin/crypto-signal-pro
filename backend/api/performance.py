"""信號績效統計 API。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/signal-performance", tags=["performance"])


@router.get("")
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


@router.post("/refresh")
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
