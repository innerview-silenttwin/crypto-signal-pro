"""
每日績效報告模組

每天晚上 9 點自動彙總兩大交易中心績效，透過 Telegram Bot 推播：
1. 台股交易中心（6 類股帳戶）
2. BTC 交易中心（4 策略帳戶）
"""

import asyncio
import logging
import threading
from datetime import datetime, time as dt_time

import pytz

from notifier import send_telegram

logger = logging.getLogger(__name__)

TZ = pytz.timezone("Asia/Taipei")
REPORT_HOUR = 21  # 晚上 9 點
REPORT_MINUTE = 0


# ═══════════════════════════════════════════════
# 台股交易中心績效
# ═══════════════════════════════════════════════

def _build_sector_report() -> str:
    """彙總台股 6 類股帳戶績效"""
    from sector_trader import get_all_managers
    from sector_auto_trader import get_current_price

    lines = ["\U0001f4ca <b>台股交易中心 — 每日績效報告</b>"]
    lines.append(f"時間：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}\n")

    grand_initial = 0.0
    grand_equity = 0.0
    grand_pl = 0.0
    total_wins = 0
    total_trades = 0

    for sector_id, mgr in get_all_managers().items():
        # 取得持倉即時價格
        current_prices = {}
        for symbol, hold in mgr.state.get("holdings", {}).items():
            if hold.get("qty", 0) > 0:
                price = get_current_price(symbol)
                if price:
                    current_prices[symbol] = price

        s = mgr.get_summary(current_prices)
        initial = s["initial_balance"]
        equity = s["equity"]
        pl = s["total_pl"]
        pl_pct = s["total_pl_pct"]
        stats = s["stats"]
        holding_count = sum(1 for h in s["holdings"].values() if h.get("qty", 0) > 0)

        grand_initial += initial
        grand_equity += equity
        grand_pl += pl
        total_wins += stats["wins"]
        total_trades += stats["total_trades"]

        # 損益 emoji
        emoji = "\U0001f7e2" if pl >= 0 else "\U0001f534"
        lines.append(
            f"{emoji} <b>{s['sector_name']}</b>　"
            f"${equity:,.0f}　{pl_pct:+.2f}%　"
            f"持倉{holding_count}檔　"
            f"勝率{stats['win_rate']:.0f}%({stats['total_trades']}筆)"
        )

    # 台股總計
    grand_pct = (grand_pl / grand_initial * 100) if grand_initial else 0
    win_rate = (total_wins / total_trades * 100) if total_trades else 0
    g_emoji = "\U0001f4b0" if grand_pl >= 0 else "\U0001f4b8"
    lines.append(f"\n{g_emoji} <b>台股合計</b>")
    lines.append(f"總權益：${grand_equity:,.0f}（初始 ${grand_initial:,.0f}）")
    lines.append(f"累積損益：${grand_pl:,.0f}（{grand_pct:+.2f}%）")
    lines.append(f"整體勝率：{win_rate:.0f}%（{total_trades} 筆）")

    return "\n".join(lines)


# ═══════════════════════════════════════════════
# BTC 交易中心績效
# ═══════════════════════════════════════════════

def _build_btc_report() -> str:
    """彙總 BTC 4 策略帳戶績效"""
    from btc_auto_trader import btc_trader, fetch_btc_price, STRATEGIES

    price = fetch_btc_price()
    summary = btc_trader.account.get_summary(price)

    lines = ["\n\U000020bf <b>BTC 交易中心 — 每日績效報告</b>"]
    lines.append(f"BTC 現價：${price:,.0f}\n" if price else "")

    equity = summary["equity"]
    initial = summary["initial_balance"]
    ret_pct = summary["total_return_pct"]
    unrealized = summary["unrealized_pl"]

    # 各策略持倉狀態
    holdings = btc_trader.account.state.get("holdings", {})
    history = btc_trader.account.state.get("history", [])

    for s in STRATEGIES:
        hold_key = f"BTC/USDT_{s['id']}"
        hold = holdings.get(hold_key)
        if hold and hold["qty"] > 0 and price:
            cost = hold["qty"] * hold["avg_price"]
            mv = hold["qty"] * price
            pnl = mv - cost
            pnl_pct = (pnl / cost * 100) if cost else 0
            emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
            lines.append(
                f"{emoji} {s['name']}　"
                f"{hold['qty']:.4f} BTC　"
                f"成本${hold['avg_price']:,.0f}　"
                f"損益 ${pnl:,.0f}（{pnl_pct:+.1f}%）"
            )
        else:
            lines.append(f"\u26aa {s['name']}　空倉")

    # 交易統計
    sell_trades = [h for h in history if h.get("type") == "SELL"]
    wins = sum(1 for t in sell_trades if t.get("profit", 0) > 0)
    win_rate = (wins / len(sell_trades) * 100) if sell_trades else 0
    realized = sum(t.get("profit", 0) for t in sell_trades)

    b_emoji = "\U0001f4b0" if ret_pct >= 0 else "\U0001f4b8"
    lines.append(f"\n{b_emoji} <b>BTC 合計</b>")
    lines.append(f"總權益：${equity:,.0f} USDT（初始 ${initial:,.0f}）")
    lines.append(f"總報酬：{ret_pct:+.2f}%")
    lines.append(f"已實現：${realized:,.0f}　未實現：${unrealized:,.0f}")
    lines.append(f"勝率：{win_rate:.0f}%（{len(sell_trades)} 筆）")

    return "\n".join(lines)


# ═══════════════════════════════════════════════
# 發送合併報告
# ═══════════════════════════════════════════════

def send_daily_report() -> bool:
    """組合兩大交易中心績效報告並發送 Telegram"""
    try:
        sector_report = _build_sector_report()
    except Exception as e:
        logger.error(f"台股績效報告產生失敗: {e}")
        sector_report = "\u26a0\ufe0f 台股績效報告產生失敗"

    try:
        btc_report = _build_btc_report()
    except Exception as e:
        logger.error(f"BTC 績效報告產生失敗: {e}")
        btc_report = "\u26a0\ufe0f BTC 績效報告產生失敗"

    message = f"{sector_report}\n{'─' * 28}\n{btc_report}"
    ok = send_telegram(message)
    if ok:
        logger.info("每日績效報告已發送")
    else:
        logger.warning("每日績效報告發送失敗")
    return ok


# ═══════════════════════════════════════════════
# 排程器（asyncio 背景任務）
# ═══════════════════════════════════════════════

async def daily_report_scheduler():
    """背景排程：每天晚上 9 點發送績效報告"""
    await asyncio.sleep(5)  # 等 server 完全啟動
    logger.info(f"每日績效報告排程已啟動（每天 {REPORT_HOUR}:{REPORT_MINUTE:02d}）")

    while True:
        now = datetime.now(TZ)
        # 計算距離下一個 21:00 的秒數
        target = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
        if now >= target:
            # 今天已過，排到明天
            target = target.replace(day=target.day + 1)
            # 處理月末
            import calendar
            if target.day > calendar.monthrange(target.year, target.month)[1]:
                if target.month == 12:
                    target = target.replace(year=target.year + 1, month=1, day=1)
                else:
                    target = target.replace(month=target.month + 1, day=1)

        wait_seconds = (target - now).total_seconds()
        logger.info(f"下次報告時間：{target.strftime('%Y-%m-%d %H:%M')}（{wait_seconds/3600:.1f} 小時後）")

        await asyncio.sleep(wait_seconds)

        # 用 thread 跑報告（避免阻塞 event loop）
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, send_daily_report)
