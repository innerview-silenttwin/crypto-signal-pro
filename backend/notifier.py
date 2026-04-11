"""
Telegram Bot 推播模組

使用方式：
1. Telegram 搜尋 @BotFather → /newbot → 取得 Bot Token
2. 對 Bot 發送任意訊息，然後打開：
   https://api.telegram.org/bot<TOKEN>/getUpdates
   找到 chat.id 即為你的 Chat ID
3. 在 .env 檔案加入：
   TELEGRAM_BOT_TOKEN=你的bot_token
   TELEGRAM_CHAT_ID=你的chat_id
4. 交易執行時會自動發送通知
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _get_config() -> tuple:
    """從環境變數取得 Telegram 設定"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    return token, chat_id


def send_telegram(message: str) -> bool:
    """發送 Telegram 訊息

    Args:
        message: 訊息內容

    Returns:
        是否發送成功
    """
    token, chat_id = _get_config()
    if not token or not chat_id:
        logger.debug("TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未設定，跳過通知")
        return False

    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            logger.debug(f"Telegram 通知已送出: {message[:50]}")
            return True
        else:
            logger.warning(f"Telegram 通知失敗: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        logger.warning(f"Telegram 通知例外: {e}")
        return False


def notify_trade(sector_name: str, symbol: str, stock_name: str,
                 trade_type: str, price: float, qty: int,
                 signal_desc: str, profit: float = None) -> bool:
    """交易通知格式化並發送

    Args:
        sector_name: 類股名稱
        symbol: 股票代碼
        stock_name: 股票名稱
        trade_type: BUY / SELL
        price: 成交價
        qty: 成交股數
        signal_desc: 信號描述
        profit: 已實現損益（賣出時才有）
    """
    emoji = "\U0001f7e2" if trade_type == "BUY" else "\U0001f534"
    action = "買入" if trade_type == "BUY" else "賣出"
    amount = round(price * qty)

    code = symbol.replace(".TW", "").replace(".TWO", "")
    stock_url = f"https://tw.stock.yahoo.com/quote/{code}.TW"

    lines = [
        f"{emoji} <b>{action}通知</b> [{sector_name}]",
        f"標的：<a href=\"{stock_url}\">{stock_name}({code})</a>",
        f"價格：{price:.2f} × {qty}股",
        f"金額：${amount:,}",
        f"原因：{signal_desc}",
    ]

    if profit is not None:
        pnl_emoji = "\U0001f4c8" if profit >= 0 else "\U0001f4c9"
        lines.append(f"損益：{pnl_emoji} ${profit:,.0f}")

    return send_telegram("\n".join(lines))
