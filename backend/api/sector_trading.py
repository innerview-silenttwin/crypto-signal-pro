"""類股虛擬交易 API（6 個獨立交易中心）。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。

**路由順序敏感**：
FastAPI 按註冊順序匹配 path，含參數的 /{sector_id}/... 會吃掉任何前綴相同的固定路徑。
因此本檔內 endpoint 必須維持原始順序：
  1. 集合查詢類（/sectors）
  2. /auto-trader/* 守護程式控制
  3. /{sector_id}/* 個別類股
否則 /auto-trader/start 會被 /{sector_id}/start 誤判為 sector_id="auto-trader"。
"""

import asyncio

from fastapi import APIRouter

from api._utils import _sanitize
from sector_trader import get_manager, get_all_managers as get_all_sector_managers
from sector_auto_trader import (
    auto_trader as sector_auto_trader,
    get_current_price,
    fetch_signal_data,
    build_layers,
)
from signals.aggregator import SignalAggregator

router = APIRouter(prefix="/api/sector-trading", tags=["sector-trading"])


# ── helper ───────────────────────────────────────────────────────────
# _sanitize 已抽至 api/_utils.py（A6b cleanup），下面 caller 直接用 import 的版本


def _require_manager(sector_id: str):
    """取 sector manager；找不到回 (None, error_dict)，否則回 (mgr, None)。

    保留原行為：未知 sector_id 回 200 + {"error": ...}（非 404），前端錯誤判斷邏輯不變。
    用法：`mgr, err = _require_manager(sid); if err: return err`
    """
    mgr = get_manager(sector_id)
    if not mgr:
        return None, {"error": f"未知的類股 ID: {sector_id}"}
    return mgr, None


def _compute_sector_regime(sector_id: str):
    """同步計算類股盤勢辨識；給 to_thread 用，避免阻塞 event loop。"""
    mgr, err = _require_manager(sector_id)
    if err:
        return err

    strategy = mgr.get_strategy()
    layers = build_layers(strategy)

    # ── 取 F3 effective_buy_th（與 process_sector 邏輯一致）──
    # TAIEX neutral 時 buy 門檻拉到 50；其它情境用 sector 設定值。
    # User 2026-06-04：想知道個股「離 BUY/SELL 觸發還差幾分」
    from sector_auto_trader import fetch_taiex_regime
    buy_th = strategy["buy_threshold"]
    sell_th = strategy["sell_threshold"]
    taiex_regime = fetch_taiex_regime()
    effective_buy_th = max(buy_th, 50) if taiex_regime == "neutral" else buy_th

    results = {}
    for symbol in mgr.state.get("stocks", []):
        df = fetch_signal_data(symbol)
        if df is None:
            results[symbol] = {"regime": "無數據", "details": {}}
            continue

        aggregator = SignalAggregator(weights=strategy["weights"])
        signal = aggregator.analyze(
            df.copy(), symbol, "1d",
            layers=layers, sector_id=sector_id,
        )

        bs = round(float(signal.buy_score), 1)
        ss = round(float(signal.sell_score), 1)

        modifier = signal.layer_modifiers[0] if signal.layer_modifiers else None
        details = _sanitize(modifier.details) if modifier else {}
        results[symbol] = {
            "name": mgr.stocks.get(symbol, symbol),
            "price": round(float(df['close'].iloc[-1]), 2),
            "regime": signal.regime or "未知",
            "buy_score": bs,
            "sell_score": ss,
            "raw_buy_score": round(float(signal.raw_buy_score), 1),
            "raw_sell_score": round(float(signal.raw_sell_score), 1),
            "direction": signal.direction,
            "signal_level": signal.signal_level,
            "details": details,
            "reason": modifier.reason if modifier else "",
            # ── 健康狀況：離觸發還差多少 ──
            "buy_threshold": effective_buy_th,           # 實際使用的買入門檻（含 F3 調整）
            "sell_threshold": sell_th,                   # 賣出門檻
            "buy_gap": round(effective_buy_th - bs, 1),  # 正數=還差幾分、負數=已過門檻
            "sell_gap": round(sell_th - ss, 1),
            "taiex_regime": taiex_regime,                # 提示 F3 是否激活
        }

    return {
        "sector_id": sector_id,
        "stocks": results,
        # sector 層 metadata，前端不用每檔重複算
        "buy_threshold": effective_buy_th,
        "sell_threshold": sell_th,
        "taiex_regime": taiex_regime,
    }


# ── 1. 集合查詢 ───────────────────────────────────────────────────────

@router.get("/sectors")
async def list_sectors():
    """列出所有類股及其摘要"""
    results = []
    for sector_id, mgr in get_all_sector_managers().items():
        current_prices = {}
        for symbol, hold in mgr.state.get("holdings", {}).items():
            if hold.get("qty", 0) > 0:
                price = get_current_price(symbol)
                if price:
                    current_prices[symbol] = price
        results.append(mgr.get_summary(current_prices))
    return results


# ── 處置股 ──

@router.get("/large-holders-batch")
async def get_large_holders_batch():
    """全市場大戶持股 % snapshot（給前端一次拿、本地 lookup 避免 N 個 API call）。

    回傳：
      {
        "meta": {"fetch_date": "2026-06-18", "symbol_count": 3992},
        "stocks": {"2330": {"large_pct": 85.22, "concentration": "極度集中"}, ...}
      }
    無資料回 {"meta": {...}, "stocks": {}}
    """
    try:
        from layers.large_holder import get_cache, interpret_concentration
        cache = get_cache()
        meta = cache.snapshot_meta()
        # 用 batch_snapshot 維持封裝、避免外部直接戳 _snapshot / _lock
        stocks = cache.batch_snapshot(lambda info: {
            "large_pct": info["large_pct"],
            "concentration": interpret_concentration(info["large_pct"]),
        })
        return {"meta": meta, "stocks": stocks}
    except Exception as e:
        return {"meta": {"fetch_date": None, "symbol_count": 0}, "stocks": {}, "error": str(e)}


@router.get("/large-holder/{symbol}")
async def get_large_holder(symbol: str):
    """個股大戶持股 % — 純揭露資訊，不進五面評分。

    來源：TDCC 集保戶股權分散表（每週公布、ID 歸戶）
    級距 15 = 持股 > 1000 張（即「大戶」）；級 12-14 = 400-1000 張（中大戶）

    回傳 schema：
      {
        "symbol": "2330",
        "date": "2026-06-18",         # 集保資料日
        "large_pct": 85.22,           # 大戶 (>1000 張) 占比
        "large_holders": 1484,        # 大戶戶數
        "medium_large_pct": 2.70,     # 中大戶 (400-1000 張) 占比
        "large_plus_medium_pct": 87.92, # 合計 >400 張占比
        "total_holders": 2835392,     # 總集保戶數
        "concentration": "極度集中",     # 文字描述
      }
    無資料回 {"symbol": ..., "ok": false}
    """
    try:
        from layers.large_holder import get_large_holder_info, interpret_concentration
        info = get_large_holder_info(symbol)
        if info is None:
            return {"symbol": symbol, "ok": False, "msg": "無 TDCC 集保資料"}
        return {
            "symbol": symbol,
            "ok": True,
            "date": info["date"],
            "large_pct": info["large_pct"],
            "large_holders": info["large_holders"],
            "medium_large_pct": info["medium_large_pct"],
            "medium_large_holders": info["medium_large_holders"],
            "large_plus_medium_pct": info["large_plus_medium_pct"],
            "total_holders": info["total_holders"],
            "concentration": interpret_concentration(info["large_pct"]),
        }
    except Exception as e:
        return {"symbol": symbol, "ok": False, "error": str(e)}


@router.get("/disposition-stocks")
async def get_disposition_stocks():
    """目前的處置股清單 + 完整資訊（給前端做 badge 標示 + 日期）。

    回傳 schema：
      {
        "date": "2026-06-11" or null,
        "ok": true,
        "count": 5,
        "stocks": {
          "6770": {
            "start_date": "2026-06-02",
            "end_date": "2026-06-15",
            "interval": "5分鐘",        # 或 "20分鐘"（加重處置）
            "unit_limit": 10.0,
            "total_limit": 30.0,
            ...
          },
          ...
        }
      }
    cache 未載入或 broker 不在時，回 {ok: false, stocks: {}}。
    """
    try:
        from brokers.disposition_guard import get_guard
        guard = get_guard()
        if guard is None:
            return {"date": None, "ok": False, "count": 0, "stocks": {}}
        return guard.snapshot()
    except Exception as e:
        return {"date": None, "ok": False, "count": 0, "stocks": {}, "error": str(e)}


# ── 2. 自動交易守護程式控制（必須在 {sector_id} 之前）───────────────

@router.post("/auto-trader/start")
async def start_auto_trader():
    """啟動背景自動交易"""
    ok = sector_auto_trader.start()
    return {"started": ok, **sector_auto_trader.get_status()}


@router.post("/auto-trader/stop")
async def stop_auto_trader():
    """停止背景自動交易"""
    ok = sector_auto_trader.stop()
    return {"stopped": ok, **sector_auto_trader.get_status()}


@router.get("/auto-trader/status")
async def get_auto_trader_status():
    """取得自動交易狀態"""
    return sector_auto_trader.get_status()


@router.post("/auto-trader/run-once")
async def run_auto_trader_once():
    """手動觸發一次交易檢查"""
    import threading
    t = threading.Thread(target=sector_auto_trader.run_once_now, daemon=True)
    t.start()
    return {"triggered": True, "message": "已觸發一次交易檢查，請稍後查看結果"}


# ── 3. 個別類股操作（{sector_id} 路由放最後）──────────────────────

@router.get("/{sector_id}/status")
async def get_sector_status(sector_id: str):
    """取得單一類股帳戶摘要"""
    mgr, err = _require_manager(sector_id)
    if err:
        return err
    # 統一取價：多來源比較日期，取最新的收盤價
    current_prices = {}
    for symbol, hold in mgr.state.get("holdings", {}).items():
        if hold.get("qty", 0) > 0:
            price = get_current_price(symbol)
            if price:
                current_prices[symbol] = price
    return mgr.get_summary(current_prices)


@router.post("/{sector_id}/toggle")
async def toggle_sector_trading(sector_id: str, active: bool = False):
    """啟動/停止單一類股自動交易"""
    mgr, err = _require_manager(sector_id)
    if err:
        return err
    is_active = mgr.toggle_active(active)
    return {"sector_id": sector_id, "is_active": is_active}


@router.get("/{sector_id}/history")
async def get_sector_history(sector_id: str, page: int = 1, pageSize: int = 50,
                             symbol: str = "", startDate: str = "", endDate: str = "",
                             tradeType: str = "", pnlStatus: str = ""):
    """取得單一類股交易歷史

    Args:
        pnlStatus: 篩選「realized」（已實現）/「unrealized」（未實現）/「」（全部）
    """
    mgr, err = _require_manager(sector_id)
    if err:
        return err
    # 取得目前持倉的即時價格，用於計算未實現損益
    current_prices = {}
    for sym, hold in mgr.state.get("holdings", {}).items():
        if hold.get("qty", 0) > 0:
            price = get_current_price(sym)
            if price:
                current_prices[sym] = price
    return mgr.get_history(page, pageSize, symbol, startDate, endDate,
                           trade_type=tradeType,
                           pnl_status=pnlStatus,
                           current_prices=current_prices)


@router.post("/{sector_id}/strategy")
async def update_sector_strategy(sector_id: str, strategy: dict):
    """更新類股策略設定"""
    mgr, err = _require_manager(sector_id)
    if err:
        return err
    mgr.update_strategy(strategy)
    return {"success": True, "strategy": mgr.get_strategy()}


@router.post("/{sector_id}/reset")
async def reset_sector_account(sector_id: str):
    """重置類股帳戶（保留策略）"""
    mgr, err = _require_manager(sector_id)
    if err:
        return err
    mgr.reset_account()
    return {"success": True}


@router.get("/{sector_id}/regime")
async def get_sector_regime(sector_id: str):
    """取得類股各標的即時盤勢辨識"""
    return await asyncio.to_thread(_compute_sector_regime, sector_id)


@router.get("/{sector_id}/fundamental")
async def get_sector_fundamental(sector_id: str):
    """取得類股各標的基本面 P/E 分析"""
    mgr, err = _require_manager(sector_id)
    if err:
        return err

    from layers.fundamental import fetch_twse_pe_all, get_sector_pe_stats

    symbols = mgr.state.get("stocks", [])
    all_pe = fetch_twse_pe_all()

    if not all_pe:
        return {"sector_id": sector_id, "stocks": {}, "error": "無法取得 TWSE P/E 資料"}

    stats = get_sector_pe_stats(symbols, all_pe)

    # 補上股票中文名
    for sym in stats:
        if not stats[sym].get("name"):
            stats[sym]["name"] = mgr.stocks.get(sym, sym)

    return {"sector_id": sector_id, "stocks": _sanitize(stats)}
