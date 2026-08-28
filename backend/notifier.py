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
import time

import requests

logger = logging.getLogger(__name__)

# 發送失敗時的內聯重試（被動：只在發失敗當下多試幾次、非背景輪詢）。
# 退避短、有上限——避免斷網時拖慢交易迴圈內的通知呼叫。
_SEND_BACKOFF = [2, 4]   # 重試前等待秒數；attempts = len+1 = 3 次，最壞多等 6 秒

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _get_config() -> tuple:
    """從環境變數取得 Telegram 設定"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    
    # Override chat_id with our settings
    try:
        from settings_manager import get_settings
        settings = get_settings()
        stored_chat_ids = settings.get("telegram", {}).get("chat_ids", "")
        if stored_chat_ids:
            chat_id = stored_chat_ids
    except Exception as e:
        logger.warning(f"Failed to load settings: {e}")
        
    return token, chat_id


def _redact(text, token: str) -> str:
    """遮蔽 log 字串中的 bot token——requests 例外會帶含 token 的 URL，避免 secret 寫進 log。"""
    s = str(text)
    if token:
        s = s.replace(token, "<bot-token>")
    return s


def _mask_chat(c_id) -> str:
    """chat_id 部分遮蔽（識別碼、不全寫）。"""
    c = str(c_id)
    return ("***" + c[-4:]) if len(c) > 4 else "***"


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

    chat_ids_list = [c.strip() for c in chat_id.split(",") if c.strip()]
    success = False
    
    attempts = len(_SEND_BACKOFF) + 1
    for c_id in chat_ids_list:
        sent = False
        last_err = ""
        for i in range(attempts):
            try:
                resp = requests.post(
                    TELEGRAM_API.format(token=token),
                    json={
                        "chat_id": c_id,
                        "text": message,
                        "parse_mode": "HTML",
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    # #2 成功記 INFO（token 已遮蔽）—留送達紀錄、可稽核
                    logger.info(f"Telegram 已送出至 {_mask_chat(c_id)}"
                                + (f"（第 {i+1} 次）" if i else ""))
                    success = True
                    sent = True
                    break
                last_err = f"HTTP {resp.status_code} {_redact(resp.text, token)}"
            except Exception as e:
                last_err = _redact(e, token)
            # 還沒成功且還有下一次 → 退避後重試（被動內聯、非背景）
            if i < attempts - 1:
                time.sleep(_SEND_BACKOFF[i])
        if not sent:
            logger.warning(f"Telegram 發送失敗（重試 {attempts} 次）至 "
                           f"{_mask_chat(c_id)}: {last_err}")

    return success


def notify_trade(sector_name: str, symbol: str, stock_name: str,
                 trade_type: str, price: float, qty: int,
                 signal_desc: str, profit: float = None,
                 profit_pct: float = None, broker: str = "") -> bool:
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
        profit_pct: 已實現損益百分比（賣出時才有）
        broker: broker 名稱（"sinopac" / "virtual"），用來標註交易來源
    """
    emoji = "\U0001f7e2" if trade_type == "BUY" else "\U0001f534"
    action = "買入" if trade_type == "BUY" else "賣出"
    amount = round(price * qty)

    code = symbol.replace(".TWO", "").replace(".TW", "")
    stock_url = f"https://tw.stock.yahoo.com/quote/{code}.TW"

    # 來源標籤：永豐單明確標出來、虛擬單低調標
    if broker == "sinopac":
        source_tag = "🏦 永豐 simulation"
    elif broker == "virtual":
        source_tag = "📒 系統虛擬"
    else:
        source_tag = ""

    lines = [
        f"{emoji} <b>{action}通知</b> [{sector_name}]" + (f" · {source_tag}" if source_tag else ""),
        f"標的：<a href=\"{stock_url}\">{stock_name}({code})</a>",
        f"價格：{price:.2f} × {qty}股",
        f"金額：${amount:,}",
        f"原因：{signal_desc}",
    ]

    if profit is not None:
        pnl_emoji = "\U0001f4c8" if profit >= 0 else "\U0001f4c9"
        pct_str = f"（{profit_pct:+.2f}%）" if profit_pct is not None else ""
        lines.append(f"損益：{pnl_emoji} ${profit:,.0f}{pct_str}")

    return send_telegram("\n".join(lines))
