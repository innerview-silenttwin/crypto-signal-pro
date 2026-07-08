"""處置雷達 API（純觀察、不做買賣判斷、不下單）。

- GET /api/disposition/radar
    今日「距處置還差幾次」觀察名單（紅≤1 / 橙2 / 黃3 三級）+ 已在處置中清單。
    每檔可再打既有 /api/chip-disclosure/stock?code= 看籌碼疊加。
"""

from fastapi import APIRouter

router = APIRouter(tags=["disposition-radar"])


@router.get("/api/disposition/radar")
async def get_disposition_radar(bdays: int = 30):
    """處置雷達：預判即將達處置條件的股票（純資料、不含買賣建議）。

    bdays：計數回看的交易日數（預設 30，涵蓋最長的 30 日 12 次規則）。
    """
    from disposition_radar import compute_radar
    bdays = max(10, min(int(bdays), 60))
    return compute_radar(bdays=bdays)
