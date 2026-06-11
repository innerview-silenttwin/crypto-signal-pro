"""服務的 logging 設定（單一入口，由 main.py 在啟動時呼叫一次）。

預設 root=INFO，讓各模組 logger.info/debug 真的輸出——在此之前 root 無 handler，
info/debug 被 Python 預設吞掉（只有 WARNING+ 靠 lastResort 出得來），所以歷史上
不少診斷訊息只能用 print 繞過。設好之後即可逐步把那些 print 轉回 logger。

🔴 安全鐵則：root 永遠不要設成 DEBUG。
   Telegram bot token 在 notifier.py 是放進 URL（`bot{token}/sendMessage`，走 requests→urllib3）。
   urllib3 在 INFO 只印 host（安全），但在 **DEBUG** 會印出含 token 的完整 path → 寫進 log 檔。
   要除錯個別模組，請只調那個 logger 的 level（例：logging.getLogger("sector_auto_trader")
   .setLevel(DEBUG)），不要動 root，才不會連帶把第三方 lib 的 token-bearing DEBUG 也打開。
"""

import logging

# 會在 INFO 噴大量雜訊（連線池 / 下載訊息）的第三方 logger → 壓到 WARNING。
# 註：shioaji 另在 brokers/sinopac.py、quote_provider/sinopac_provider.py 也各自壓過
#     （避免印 credentials），此處集中再保險一次。
_NOISY_THIRD_PARTY = ("urllib3", "yfinance", "ccxt", "websockets", "shioaji")
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging() -> None:
    """設定 root=INFO + 壓第三方 lib。冪等，可重複呼叫。"""
    root = logging.getLogger()
    if not root.handlers:
        # 正常情況（prod / uvicorn 預設不在 root 掛 handler）：建一個帶時間戳的 handler。
        logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    # 即使 root 已有 handler（pytest 等情境 basicConfig 會 no-op），仍確保 level=INFO；
    # 但絕不降到 DEBUG（見檔頭安全鐵則）。
    root.setLevel(logging.INFO)
    for name in _NOISY_THIRD_PARTY:
        logging.getLogger(name).setLevel(logging.WARNING)
