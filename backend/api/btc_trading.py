"""BTC 自動交易 API。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。
"""

import threading

from fastapi import APIRouter

from btc_auto_trader import btc_trader

router = APIRouter(prefix="/api/btc-trading", tags=["btc-trading"])


@router.get("/status")
async def btc_trading_status():
    """取得 BTC 交易帳戶狀態"""
    return btc_trader.get_status()


@router.post("/toggle")
async def btc_trading_toggle(active: bool = True):
    """開啟/關閉 BTC 自動交易"""
    btc_trader.account.toggle(active)
    if active and not btc_trader.is_running:
        btc_trader.start()
    return {"is_active": active, "message": f"BTC 自動交易已{'開啟' if active else '關閉'}"}


@router.post("/run-once")
async def btc_trading_run_once():
    """手動觸發一次 BTC 交易檢查"""
    if not btc_trader.account.is_active:
        return {"error": "BTC 交易未啟用，請先開啟"}
    t = threading.Thread(target=btc_trader.run_once, daemon=True)
    t.start()
    return {"triggered": True, "message": "已觸發 BTC 交易檢查"}


@router.get("/history")
async def btc_trading_history():
    """取得 BTC 交易歷史"""
    return btc_trader.account.state.get("history", [])[:50]


@router.get("/equity-curve")
async def btc_equity_curve():
    """取得 BTC 權益曲線"""
    return btc_trader.account.state.get("equity_curve", [])


@router.get("/flow-info")
async def btc_flow_info():
    """取得最新恐懼貪婪指數與資金費率"""
    try:
        from layers.crypto_flow import CryptoFlowLayer
        import pandas as pd
        layer = CryptoFlowLayer()
        layer._load_data()
        now = pd.Timestamp.now()
        fng = layer._get_fng(now)
        fr_pct = layer._get_funding_rate_percentile(now)
        if fng <= 25:
            fng_class = "極度恐懼"
        elif fng <= 45:
            fng_class = "恐懼"
        elif fng <= 55:
            fng_class = "中性"
        elif fng <= 75:
            fng_class = "貪婪"
        else:
            fng_class = "極度貪婪"
        return {"fear_greed": fng, "fng_class": fng_class, "funding_rate_pct": fr_pct}
    except Exception as e:
        return {"fear_greed": 50, "fng_class": "N/A", "funding_rate_pct": 50, "error": str(e)}
