"""通知 / 報告 API。

目前只有每日績效報告觸發。將來其他「觸發發訊」類的 endpoint 可以放在這裡。
"""

import threading

from fastapi import APIRouter

router = APIRouter(tags=["notifications"])


@router.post("/api/daily-report/send")
async def trigger_daily_report():
    """手動觸發每日績效報告"""
    from daily_report import send_daily_report
    t = threading.Thread(target=send_daily_report, daemon=True)
    t.start()
    return {"triggered": True, "message": "績效報告已觸發，稍後將發送至 Telegram"}
