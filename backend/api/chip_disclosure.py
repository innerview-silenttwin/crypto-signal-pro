"""大盤籌碼揭露 API（Phase 1：市場層級，純揭露不判斷）。

- GET /api/chip-disclosure/market?days=20
    台指期三大法人未平倉淨額 + 大盤三大法人買賣超 + 選擇權 P/C 比
"""

from fastapi import APIRouter

router = APIRouter(tags=["chip-disclosure"])


@router.get("/api/chip-disclosure/market")
async def get_market_chip(days: int = 20):
    """市場層級籌碼揭露（純資料、不含買賣建議）。days 上限 120。"""
    from chip_disclosure import market_overview
    days = max(5, min(int(days), 120))
    return market_overview(days)


@router.get("/api/chip-disclosure/stock")
async def get_stock_chip(code: str, days: int = 20):
    """個股層級籌碼揭露：三大法人連買 / 借券 / 當沖 / 大戶（純資料、不判斷）。"""
    from chip_disclosure import stock_overview
    code = (code or "").strip()
    if not code:
        return {"error": "empty_code"}
    days = max(5, min(int(days), 120))
    return stock_overview(code, days)
