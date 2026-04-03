"""
基本面 P/E 分析層 (Fundamental Analysis Layer)

用 TWSE Open Data 抓取本益比、殖利率、股價淨值比：
1. 抓取全市場 P/E 數據（BWIBBU_ALL）
2. 計算類股內 P/E 百分位排名
3. 低估加分、高估減分

資料來源：https://www.twse.com.tw/exchangeReport/BWIBBU_ALL
"""

import time
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

import numpy as np
import pandas as pd
import requests

from .base import BaseLayer, LayerModifier, LayerRegistry

logger = logging.getLogger(__name__)


# ── TWSE P/E 資料快取 ──

_pe_cache: Dict = {}       # {"data": {symbol: {...}}, "time": float}
PE_CACHE_TTL = 3600 * 4    # 4 小時（盤中資料每日更新，不需太頻繁）

_rev_cache: Dict = {}      # 營收快取
REV_CACHE_TTL = 3600 * 24

def fetch_twse_revenue_all() -> Dict[str, dict]:
    """
    從 TWSE OpenAPI 抓取所有上市公司最新營收（MoM, YoY）
    """
    now = time.time()
    if _rev_cache and now - _rev_cache.get("time", 0) < REV_CACHE_TTL:
        return _rev_cache["data"]

    url = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
    try:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        except requests.exceptions.SSLError:
            resp = requests.get(url, timeout=10, verify=False, headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        result = {}
        for row in data:
            code = row.get("公司代號")
            mom = row.get("營業收入-上月比較增減(%)")
            yoy = row.get("營業收入-去年同月增減(%)")
            sector = row.get("產業別", "")
            if code and mom and yoy:
                try:
                    result[code] = {"mom": float(mom), "yoy": float(yoy), "sector": sector.strip()}
                except ValueError:
                    pass
        if result:
            _rev_cache["data"] = result
            _rev_cache["time"] = now
            logger.info(f"TWSE 營收資料已更新: {len(result)} 筆")
            return result
    except Exception as e:
        logger.warning(f"TWSE 營收抓取失敗: {e}")
    return _rev_cache.get("data", {})


def _strip_tw(symbol: str) -> str:
    """2330.TW → 2330"""
    return symbol.replace(".TW", "").replace(".TWO", "")


def _safe_float(val) -> Optional[float]:
    """安全解析浮點數"""
    try:
        v = str(val).strip().replace(",", "")
        return float(v) if v and v not in ("-", "--", "") else None
    except (ValueError, AttributeError):
        return None


def fetch_twse_pe_all() -> Dict[str, dict]:
    """
    從 TWSE OpenAPI 抓取全市場本益比/殖利率/股價淨值比

    Returns:
        {stock_code: {"pe": float, "dy": float, "pb": float, "name": str}}
    """
    now = time.time()
    if _pe_cache and now - _pe_cache.get("time", 0) < PE_CACHE_TTL:
        return _pe_cache["data"]

    result = {}

    # 優先使用 OpenAPI（不需日期參數，直接回傳最新資料）
    openapi_url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
    try:
        try:
            resp = requests.get(openapi_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        except requests.exceptions.SSLError:
            resp = requests.get(openapi_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}, verify=False)
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            for row in data:
                code = row.get("Code", "").strip()
                name = row.get("Name", "").strip()
                if not code:
                    continue
                result[code] = {
                    "name": name,
                    "pe": _safe_float(row.get("PEratio")),
                    "dy": _safe_float(row.get("DividendYield")),
                    "pb": _safe_float(row.get("PBratio")),
                }
            if result:
                _pe_cache["data"] = result
                _pe_cache["time"] = now
                logger.info(f"TWSE P/E 資料已更新 (OpenAPI): {len(result)} 筆")
                return result
    except Exception as e:
        logger.warning(f"TWSE OpenAPI P/E 抓取失敗: {e}")

    logger.error("TWSE P/E OpenAPI 抓取失敗")
    return _pe_cache.get("data", {})


def get_sector_pe_stats(symbols: list, all_pe: Dict[str, dict]) -> dict:
    """
    計算類股 P/E 統計：中位數、百分位

    Args:
        symbols: 類股內的股票代碼 (e.g. ["2330.TW", ...])
        all_pe: fetch_twse_pe_all() 回傳的全市場資料

    Returns:
        {symbol: {"pe": float, "percentile": float, "valuation": str, ...}}
    """
    # 收集類股內有效 P/E
    sector_data = {}
    pe_values = []

    for sym in symbols:
        code = _strip_tw(sym)
        info = all_pe.get(code)
        if info and info["pe"] is not None and info["pe"] > 0:
            sector_data[sym] = info
            pe_values.append(info["pe"])

    if not pe_values:
        return {}

    pe_arr = np.array(pe_values)
    sector_median = float(np.median(pe_arr))
    sector_mean = float(np.mean(pe_arr))

    result = {}
    for sym in symbols:
        code = _strip_tw(sym)
        info = all_pe.get(code)
        if not info or info["pe"] is None or info["pe"] <= 0:
            result[sym] = {
                "pe": None, "dy": info["dy"] if info else None,
                "pb": info["pb"] if info else None,
                "percentile": None, "valuation": "無數據",
                "sector_median_pe": sector_median,
            }
            continue

        pe = info["pe"]
        # 百分位：在類股中 P/E 排第幾 (越低越好)
        # percentile = 低於此 P/E 的股票佔比 → 低 percentile = 低估
        percentile = float(np.sum(pe_arr < pe) / len(pe_arr) * 100)

        # 估值分類
        if percentile <= 20:
            valuation = "明顯低估"
        elif percentile <= 40:
            valuation = "偏低估"
        elif percentile <= 60:
            valuation = "合理"
        elif percentile <= 80:
            valuation = "偏高估"
        else:
            valuation = "明顯高估"

        result[sym] = {
            "pe": pe,
            "dy": info.get("dy"),
            "pb": info.get("pb"),
            "name": info.get("name", ""),
            "percentile": round(percentile, 1),
            "valuation": valuation,
            "sector_median_pe": round(sector_median, 2),
            "sector_mean_pe": round(sector_mean, 2),
        }

    return result


class FundamentalLayer(BaseLayer):
    """基本面 P/E 分析層"""

    def __init__(self, enabled: bool = True, **kwargs):
        super().__init__("fundamental", enabled)
        self._sector_cache: Dict[str, dict] = {}  # sector_id -> pe_stats

    def compute_modifier(self, symbol: str, df: pd.DataFrame,
                         sector_id: str = "") -> LayerModifier:
        if not self.enabled:
            return LayerModifier(layer_name=self.name, active=False,
                                 reason="基本面層未啟用")

        # 取得全市場 P/E 數據
        all_pe = fetch_twse_pe_all()
        if not all_pe:
            return LayerModifier(layer_name=self.name, active=False,
                                 reason="無法取得 TWSE P/E 資料")

        # 取得該股票的 P/E
        code = _strip_tw(symbol)
        info = all_pe.get(code)

        if not info or info["pe"] is None or info["pe"] <= 0:
            return LayerModifier(
                layer_name=self.name, active=False,
                reason=f"{symbol} 無本益比資料",
                details={"pe": None, "dy": info["dy"] if info else None},
            )

        pe = info["pe"]
        dy = info.get("dy")  # 殖利率
        pb = info.get("pb")  # 股價淨值比
        
        # 抓取營收資料 (MoM, YoY)
        all_rev = fetch_twse_revenue_all()
        rev_info = all_rev.get(code, {})
        mom = rev_info.get("mom")
        yoy = rev_info.get("yoy")

        result = LayerModifier(layer_name=self.name)
        result.details = {
            "pe": pe,
            "dy": dy,
            "pb": pb,
            "mom": mom,
            "yoy": yoy,
            "name": info.get("name", ""),
        }

        # ── 根據 P/E 調整分數 ──
        # 低本益比 = 低估 → 買入加分
        # 高本益比 = 高估 → 買入減分

        if pe < 8:
            # 極低估：強力加分
            result.buy_multiplier = 1.25
            result.sell_multiplier = 0.7
            result.buy_offset = 8.0
            result.reason = f"P/E={pe:.1f} 極低估，基本面強力支撐買入"
        elif pe < 12:
            # 低估：適度加分
            result.buy_multiplier = 1.15
            result.sell_multiplier = 0.85
            result.buy_offset = 4.0
            result.reason = f"P/E={pe:.1f} 偏低估，基本面支撐"
        elif pe < 20:
            # 合理：不調整
            result.buy_multiplier = 1.0
            result.sell_multiplier = 1.0
            result.reason = f"P/E={pe:.1f} 估值合理"
        elif pe < 30:
            # 偏高估：稍微減分
            result.buy_multiplier = 0.85
            result.sell_multiplier = 1.1
            result.reason = f"P/E={pe:.1f} 偏高估，謹慎追高"
        else:
            # 極高估：大幅減分
            result.buy_multiplier = 0.65
            result.sell_multiplier = 1.25
            result.sell_offset = 5.0
            result.reason = f"P/E={pe:.1f} 明顯高估，注意估值風險"

        # ── 殖利率加分（高殖利率 = 防禦性強）──
        if dy is not None and dy > 0:
            if dy >= 5.0:
                result.buy_offset = result.buy_offset + 5.0
                result.reason += f"｜殖利率{dy:.1f}%（高息防禦）"
            elif dy >= 3.0:
                result.buy_offset = result.buy_offset + 2.0
                result.reason += f"｜殖利率{dy:.1f}%"

        result.details["valuation_action"] = result.reason

        return result


# 註冊到 LayerRegistry
LayerRegistry.register("fundamental", FundamentalLayer)
