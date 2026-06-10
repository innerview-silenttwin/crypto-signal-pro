"""主動式 ETF 排行 API。

從 api/screener.py 拆出（A4 cleanup）——路徑 /api/active-etf-ranking 不在
screener 的 prefix="/api/screener" 下，故獨立成 router，讓 screener 能用乾淨 prefix。
行為、回傳 schema 與搬家前完全相同。
"""

from fastapi import APIRouter

router = APIRouter(tags=["active-etf"])


@router.get("/api/active-etf-ranking")
async def get_active_etf_ranking():
    """取得主動式 ETF 持股排行（被領先大盤 ETF 重倉的台股）"""
    from layers.active_etf import get_active_etf_ranking as _get_ranking
    return _get_ranking()
