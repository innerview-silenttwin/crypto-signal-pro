"""投資諮詢 API。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from api._utils import _sanitize

router = APIRouter(tags=["consultation"])


class ConsultationRequest(BaseModel):
    symbol: str           # 股票代碼（如 "2317" 或 "2317.TW"）
    buy_price: float      # 買入均價（元）
    quantity: int         # 持有張數


@router.post("/api/consultation")
async def get_consultation(req: ConsultationRequest):
    """
    投資諮詢：根據持倉條件比對歷史類似盤勢/技術/籌碼情況，
    推算加碼 / 持有 / 減碼 / 出清建議
    """
    from consultation import consult_position
    try:
        result = consult_position(
            symbol=req.symbol,
            buy_price=req.buy_price,
            quantity=req.quantity,
        )
        return _sanitize(result)
    except Exception as e:
        logging.error(f"諮詢系統錯誤 {req.symbol}: {e}", exc_info=True)
        return {"error": str(e)}
