"""回測系統 API。

從 main.py 拆出來的純搬家——所有 endpoint 路徑、行為、回傳 schema 完全相同。
連同私有 helper（_load_task_status、_save_task_status、_execute_backtest、_save_backtest_result、
fetch_ohlcv_for_backtest）與 module-level 狀態（_backtest_tasks、_backtest_executor、
BACKTEST_* 常量）一起搬，因為這些只被回測 endpoint 用，不需要留在 main.py。
"""

import asyncio
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict

import pandas as pd
import yfinance as yf
from fastapi import APIRouter

from backtest_engine import BacktestEngine
from indicators.registry import create_all_indicators, get_indicator_keys_map

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


# ── module-level 狀態 ───────────────────────────────────────────────

_backtest_executor = ThreadPoolExecutor(max_workers=2)
_backtest_tasks: Dict[str, Dict] = {}
BACKTEST_DATA_DIR = Path(__file__).parent.parent / "data" / "backtest"
BACKTEST_DATA_DIR.mkdir(exist_ok=True)
BACKTEST_INDEX_FILE = BACKTEST_DATA_DIR / "backtest_index.json"
BACKTEST_STATUS_FILE = BACKTEST_DATA_DIR / "task_status.json"


# ── helper ───────────────────────────────────────────────────────────

def _load_task_status() -> Dict:
    """從磁碟載入任務狀態（防止 server reload 丟失）"""
    if BACKTEST_STATUS_FILE.exists():
        try:
            with open(BACKTEST_STATUS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_task_status(task_id: str, status_data: dict):
    """將任務狀態持久化到磁碟"""
    all_status = _load_task_status()
    all_status[task_id] = status_data
    # 只保留最近 50 筆
    if len(all_status) > 50:
        sorted_keys = sorted(all_status.keys())
        for k in sorted_keys[:-50]:
            del all_status[k]
    with open(BACKTEST_STATUS_FILE, "w") as f:
        json.dump(all_status, f, ensure_ascii=False)


def fetch_ohlcv_for_backtest(symbol: str, period: str = "2y") -> pd.DataFrame:
    # Use yfinance for backtesting data
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval="1d")
    if df.empty:
        return None
    df.columns = [c.lower() for c in df.columns]
    df = df[['open', 'high', 'low', 'close', 'volume']].copy()
    df.index = pd.to_datetime(df.index.date)
    return df


def _execute_backtest(task_id: str, payload: dict):
    """在背景執行緒中執行回測（同步）"""
    try:
        symbol = payload["symbol"]
        period = payload.get("period", "2y")

        def progress_callback(progress, completed, total):
            update = {
                "progress": progress,
                "completed_combos": completed,
                "total_combos": total,
            }
            _backtest_tasks[task_id].update(update)

        engine = BacktestEngine(
            initial_capital=payload.get("initial_capital", 1_000_000),
            commission_rate=payload.get("commission_rate", 0.001425),
            tax_rate=payload.get("tax_rate", 0.003),
        )

        _backtest_tasks[task_id]['status'] = 'fetching_data'
        df = fetch_ohlcv_for_backtest(symbol, period)
        if df is None:
            raise ValueError(f"無法取得 {symbol} 的歷史資料")
        time.sleep(1)  # 遵守 yfinance 速率限制

        _backtest_tasks[task_id]['status'] = 'preparing_signals'
        all_indicators = create_all_indicators(include_new=True)
        indicator_keys_map = get_indicator_keys_map(all_indicators)
        indicator_keys = list(indicator_keys_map.values())

        df_signals = engine.prepare_signals(df.copy(), all_indicators, indicator_keys_map)

        _backtest_tasks[task_id]['status'] = 'running_combos'
        results = engine.run_all_combos_with_progress(
            df_signals, indicator_keys,
            payload.get("min_combo_size", 2),
            payload.get("max_combo_size", 3),  # Default to 3 for speed
            progress_callback
        )

        buy_hold_return = float((df.iloc[-1]['close'] / df.iloc[engine.warmup_period]['close'] - 1) * 100) if len(df) > engine.warmup_period else 0.0

        symbol_name = payload.get("symbol_name", "")
        if not symbol_name:
            # fetch_stock_name 住在 main.py，lazy import 避循環依賴
            from main import fetch_stock_name
            symbol_name = fetch_stock_name(symbol) or ""

        result_data = {
            "symbol": symbol, "symbol_name": symbol_name, "period": period,
            "total_combos": len(results),
            "buy_and_hold_return": round(buy_hold_return, 2),
            "results": results,
        }
        _backtest_tasks[task_id].update({"status": "completed", "progress": 100})
        _save_backtest_result(task_id, result_data)
        _save_task_status(task_id, {"status": "completed", "progress": 100})

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        _backtest_tasks[task_id].update({"status": "failed", "error": str(e)})
        _save_task_status(task_id, {"status": "failed", "error": str(e)})


def _save_backtest_result(task_id: str, data: dict):
    # 儲存完整結果
    with open(BACKTEST_DATA_DIR / f"{task_id}.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    # 更新索引檔
    index_data = {"records": []}
    if BACKTEST_INDEX_FILE.exists():
        with open(BACKTEST_INDEX_FILE, "r", encoding="utf-8") as f:
            try:
                index_data = json.load(f)
            except json.JSONDecodeError:
                pass

    best_result = data["results"][0] if data["results"] else {}
    index_data["records"].insert(0, {
        "task_id": task_id,
        "symbol": data["symbol"],
        "symbol_name": data.get("symbol_name", ""),
        "period": data["period"],
        "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "status": "completed",
        "best_combo": best_result.get("combo", []),
        "best_return": best_result.get("total_return_pct", 0),
        "buy_hold_return_pct": data["buy_and_hold_return"]
    })
    with open(BACKTEST_INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=4)


# ── endpoints ──────────────────────────────────────────────────────

@router.post("/run")
async def run_backtest(payload: dict):
    task_id = f"bt_{payload['symbol'].replace('.', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _backtest_tasks[task_id] = {"status": "queued", "progress": 0}

    loop = asyncio.get_event_loop()
    loop.run_in_executor(_backtest_executor, _execute_backtest, task_id, payload)

    return {"status": "started", "task_id": task_id}


@router.get("/status/{task_id}")
async def get_backtest_status(task_id: str):
    # 優先從記憶體取（任務進行中）
    task = _backtest_tasks.get(task_id)
    if task:
        return {k: v for k, v in task.items() if k != "result"}
    # 記憶體沒有（server 可能 reload 過）→ 從磁碟讀
    disk_status = _load_task_status().get(task_id)
    if disk_status:
        return disk_status
    return {"status": "not_found"}


@router.get("/result/{task_id}")
async def get_backtest_result(task_id: str):
    # 直接從檔案讀取完整結果（不存記憶體，避免 JSON 序列化問題）
    result_file = BACKTEST_DATA_DIR / f"{task_id}.json"
    if result_file.exists():
        with open(result_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"status": "not_found_or_not_completed"}


@router.get("/history")
async def get_backtest_history():
    if not BACKTEST_INDEX_FILE.exists():
        return {"records": []}
    with open(BACKTEST_INDEX_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"records": []}


@router.delete("/history/{task_id}")
async def delete_backtest_record(task_id: str):
    """刪除單筆回測紀錄（索引 + 結果檔 + 狀態）"""
    # 刪除結果檔
    result_file = BACKTEST_DATA_DIR / f"{task_id}.json"
    if result_file.exists():
        result_file.unlink()

    # 從索引中移除
    if BACKTEST_INDEX_FILE.exists():
        try:
            with open(BACKTEST_INDEX_FILE, "r", encoding="utf-8") as f:
                index_data = json.load(f)
            index_data["records"] = [r for r in index_data.get("records", []) if r.get("task_id") != task_id]
            with open(BACKTEST_INDEX_FILE, "w", encoding="utf-8") as f:
                json.dump(index_data, f, ensure_ascii=False, indent=4)
        except (json.JSONDecodeError, OSError):
            pass

    # 從狀態檔移除
    all_status = _load_task_status()
    if task_id in all_status:
        del all_status[task_id]
        with open(BACKTEST_STATUS_FILE, "w") as f:
            json.dump(all_status, f, ensure_ascii=False)

    # 從記憶體移除
    _backtest_tasks.pop(task_id, None)

    return {"status": "deleted", "task_id": task_id}


@router.get("/stats")
async def get_backtest_stats():
    """
    跨所有回測結果統計指標組合排行。
    掃描所有結果檔，對每個 combo 統計：
    - 在哪些股票拿到第 1 名
    - 平均報酬率、平均勝率
    - 出現在回測中的次數
    同一股票有多次回測時，只取最新一次（避免重複計算）。
    """
    if not BACKTEST_INDEX_FILE.exists():
        return {"combos": []}

    try:
        with open(BACKTEST_INDEX_FILE, "r", encoding="utf-8") as f:
            index_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"combos": []}

    records = index_data.get("records", [])
    if not records:
        return {"combos": []}

    # 同一股票+期間只取最新一筆（index 已按時間倒序）
    seen = set()
    unique_tasks = []
    for r in records:
        key = f"{r['symbol']}_{r['period']}"
        if key not in seen:
            seen.add(key)
            unique_tasks.append(r)

    # combo_key → 統計資料
    combo_stats = defaultdict(lambda: {
        "first_place": [],       # 拿到第 1 名的 [{symbol, symbol_name, return}]
        "appearances": 0,        # 出現在幾次回測中
        "total_return": 0.0,
        "total_win_rate": 0.0,
        "total_sharpe": 0.0,
        "best_return": -999,
        "best_symbol": "",
        "best_symbol_name": "",
    })

    stock_count = 0
    for task_rec in unique_tasks:
        result_file = BACKTEST_DATA_DIR / f"{task_rec['task_id']}.json"
        if not result_file.exists():
            continue

        try:
            with open(result_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        results = data.get("results", [])
        if not results:
            continue

        symbol = data.get("symbol", "")
        symbol_name = data.get("symbol_name", "")
        stock_count += 1

        for i, r in enumerate(results[:50]):  # 只看前 50 名
            combo_key = "|".join(sorted(r.get("combo", [])))
            if not combo_key:
                continue
            s = combo_stats[combo_key]
            s["appearances"] += 1
            s["total_return"] += r.get("total_return_pct", 0)
            s["total_win_rate"] += r.get("win_rate", 0)
            s["total_sharpe"] += r.get("sharpe_ratio", 0)

            ret = r.get("total_return_pct", 0)
            if ret > s["best_return"]:
                s["best_return"] = ret
                s["best_symbol"] = symbol
                s["best_symbol_name"] = symbol_name

            if i == 0:  # 第 1 名
                s["first_place"].append({
                    "symbol": symbol,
                    "symbol_name": symbol_name,
                    "return_pct": r.get("total_return_pct", 0),
                })

            # 儲存 combo 顯示名稱（取一次就好）
            if "combo" not in s:
                s["combo"] = r.get("combo", [])
                s["combo_display"] = r.get("combo_display", r.get("combo", []))

    # 排序：先按 first_place 次數 desc，再按平均報酬 desc
    ranked = []
    for key, s in combo_stats.items():
        avg_ret = s["total_return"] / s["appearances"] if s["appearances"] else 0
        avg_wr = s["total_win_rate"] / s["appearances"] if s["appearances"] else 0
        avg_sharpe = s["total_sharpe"] / s["appearances"] if s["appearances"] else 0
        ranked.append({
            "combo": s.get("combo", key.split("|")),
            "combo_display": s.get("combo_display", key.split("|")),
            "first_count": len(s["first_place"]),
            "first_place": s["first_place"],
            "appearances": s["appearances"],
            "avg_return_pct": round(avg_ret, 2),
            "avg_win_rate": round(avg_wr, 1),
            "avg_sharpe": round(avg_sharpe, 2),
            "best_return_pct": round(s["best_return"], 2),
            "best_symbol": s["best_symbol"],
            "best_symbol_name": s["best_symbol_name"],
        })

    ranked.sort(key=lambda x: (x["first_count"], x["avg_return_pct"]), reverse=True)

    return {"combos": ranked[:30], "stock_count": stock_count}
