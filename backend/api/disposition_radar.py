"""處置雷達 API（純觀察、不做買賣判斷、不下單）。

- GET /api/disposition/radar
    今日「距處置還差幾次」觀察名單（紅≤1 / 橙2 / 黃3 三級）+ 已在處置中清單。
    每檔可再打既有 /api/chip-disclosure/stock?code= 看籌碼疊加。
"""

from fastapi import APIRouter

router = APIRouter(tags=["disposition-radar"])


# 同步 def（非 async）：compute_radar 是阻塞式（~5 次序列網路呼叫、每次最長 30s），
# FastAPI 會把同步 handler 丟到 threadpool 執行，不會卡住事件迴圈。
@router.get("/api/disposition/radar")
def get_disposition_radar(bdays: int = 30):
    """處置雷達：預判即將達處置條件的股票（純資料、不含買賣建議）。

    bdays：計數回看的交易日數。下限 30——30 日 12 次規則需完整 30 個交易日，
    低於此會讓抓取窗短於計數窗、使 D 規則靜默少算。
    """
    from disposition_radar import compute_radar
    bdays = max(30, min(int(bdays), 60))
    return compute_radar(bdays=bdays)
