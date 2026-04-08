"""
基本面分析層 (Fundamental Analysis Layer)

核心改進：成長股 vs 價值股雙軌評分
- 高營收成長股 → 用 PEG 比率 + 營收動能評估（避免被高 P/E 錯殺）
- 低成長/衰退股 → 用傳統 P/E + 殖利率評估

評分因子：
1. PEG 比率 (P/E ÷ 營收年增率) — 成長股核心指標
2. 營收動能 (YoY 連續性、加速度) — 反映未來訂單
3. 產業內 P/E 百分位 — 相對估值
4. 殖利率 — 價值股防禦力

資料來源：TWSE OpenAPI (BWIBBU_ALL + t187ap05_L)
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


# ── 統一基本面評分函數（供 screener / stock-analysis / layer 共用） ──

def compute_fundamental_score(pe: Optional[float], dy: Optional[float],
                               yoy: Optional[float], mom: Optional[float] = None,
                               pe_percentile: Optional[float] = None) -> dict:
    """
    成長股 vs 價值股雙軌評分

    成長股判定：YoY > 15%
    - 主要用 PEG 比率 (P/E ÷ YoY)，營收動能加成
    - 高 P/E 但高成長 → 不扣分

    價值股 / 低成長股：
    - 主要用 P/E 絕對值或產業百分位
    - 殖利率加分

    Returns:
        {"score": int, "advice": str, "track": "growth"|"value",
         "peg": float|None, "factors": [...]}
    """
    score = 50
    advice_parts = []
    factors = []
    track = "value"
    peg = None

    is_growth = yoy is not None and yoy > 15

    if is_growth and pe is not None and pe > 0 and yoy > 0:
        # ── 成長股軌道 ──
        track = "growth"
        peg = round(pe / yoy, 2)

        # PEG 評分（主權重）
        if peg < 0.5:
            score = 92
            advice_parts.append(f"PEG={peg} 極低估（成長遠超估值）")
        elif peg < 0.75:
            score = 82
            advice_parts.append(f"PEG={peg} 低估（成長股合理偏低）")
        elif peg < 1.0:
            score = 72
            advice_parts.append(f"PEG={peg} 合理（成長足以支撐估值）")
        elif peg < 1.5:
            score = 55
            advice_parts.append(f"PEG={peg} 偏高（成長力道尚可）")
        elif peg < 2.5:
            score = 35
            advice_parts.append(f"PEG={peg} 高估（成長不足以支撐高估值）")
        else:
            score = 20
            advice_parts.append(f"PEG={peg} 嚴重高估")
        factors.append(f"PEG={peg}")

        # 營收加速度加分（MoM > 0 代表 YoY 在擴大）
        if mom is not None and mom > 10:
            score = min(100, score + 8)
            factors.append(f"營收加速 MoM+{mom:.0f}%")
        elif mom is not None and mom > 0:
            score = min(100, score + 4)

        # 高成長額外加分
        if yoy > 50:
            score = min(100, score + 6)
            factors.append(f"營收爆發 YoY+{yoy:.0f}%")
        elif yoy > 30:
            score = min(100, score + 3)
            factors.append(f"高成長 YoY+{yoy:.0f}%")

        # 殖利率對成長股只是小加分
        if dy is not None and dy >= 3.0:
            score = min(100, score + 3)

    else:
        # ── 價值股軌道 ──
        track = "value"

        # P/E 評分（優先用產業百分位）
        if pe_percentile is not None:
            if pe_percentile <= 20:
                score = 88
                advice_parts.append(f"產業估值前20%低估")
            elif pe_percentile <= 40:
                score = 72
                advice_parts.append(f"產業內偏低估")
            elif pe_percentile <= 60:
                score = 55
                advice_parts.append(f"產業內估值合理")
            elif pe_percentile <= 80:
                score = 32
                advice_parts.append(f"產業內偏高估")
            else:
                score = 15
                advice_parts.append(f"產業內明顯高估")
        elif pe is not None and pe > 0:
            if pe < 8:
                score = 90
                advice_parts.append(f"P/E={pe:.1f} 極低估")
            elif pe < 12:
                score = 75
                advice_parts.append(f"P/E={pe:.1f} 偏低估")
            elif pe < 20:
                score = 55
                advice_parts.append(f"P/E={pe:.1f} 合理")
            elif pe < 30:
                score = 30
                advice_parts.append(f"P/E={pe:.1f} 偏高估")
            else:
                score = 15
                advice_parts.append(f"P/E={pe:.1f} 明顯高估")

        # 殖利率加分（價值股重要指標）
        if dy is not None and dy > 0:
            if dy >= 6.0:
                score = min(100, score + 12)
                factors.append(f"高息 {dy:.1f}%")
            elif dy >= 5.0:
                score = min(100, score + 10)
                factors.append(f"高息 {dy:.1f}%")
            elif dy >= 3.0:
                score = min(100, score + 5)
                factors.append(f"殖利率 {dy:.1f}%")

        # 營收動能（即使是價值股，營收衰退也要扣分）
        if yoy is not None:
            if yoy > 20:
                score = min(100, score + 10)
                factors.append(f"營收成長 YoY+{yoy:.0f}%")
            elif yoy > 0:
                score = min(100, score + 5)
            elif yoy < -20:
                score = max(0, score - 12)
                factors.append(f"營收衰退 YoY{yoy:.0f}%")
            elif yoy < -10:
                score = max(0, score - 6)
                factors.append(f"營收下滑 YoY{yoy:.0f}%")

    score = max(0, min(100, score))
    advice = "｜".join(advice_parts + factors) if (advice_parts or factors) else "資料不足"

    return {
        "score": score,
        "advice": advice,
        "track": track,
        "peg": peg,
        "factors": factors,
    }


class FundamentalLayer(BaseLayer):
    """基本面分析層（成長/價值雙軌）"""

    def __init__(self, enabled: bool = True, **kwargs):
        super().__init__("fundamental", enabled)
        self._sector_cache: Dict[str, dict] = {}  # sector_id -> pe_stats

    def compute_modifier(self, symbol: str, df: pd.DataFrame,
                         sector_id: str = "") -> LayerModifier:
        if not self.enabled:
            return LayerModifier(layer_name=self.name, active=False,
                                 reason="基本面層未啟用")

        all_pe = fetch_twse_pe_all()
        if not all_pe:
            return LayerModifier(layer_name=self.name, active=False,
                                 reason="無法取得 TWSE P/E 資料")

        code = _strip_tw(symbol)
        info = all_pe.get(code)

        if not info or info["pe"] is None or info["pe"] <= 0:
            return LayerModifier(
                layer_name=self.name, active=False,
                reason=f"{symbol} 無本益比資料",
                details={"pe": None, "dy": info["dy"] if info else None},
            )

        pe = info["pe"]
        dy = info.get("dy")
        pb = info.get("pb")

        all_rev = fetch_twse_revenue_all()
        rev_info = all_rev.get(code, {})
        mom = rev_info.get("mom")
        yoy = rev_info.get("yoy")

        # 使用統一評分函數
        fund = compute_fundamental_score(pe=pe, dy=dy, yoy=yoy, mom=mom)
        score = fund["score"]

        result = LayerModifier(layer_name=self.name)
        result.details = {
            "pe": pe, "dy": dy, "pb": pb, "mom": mom, "yoy": yoy,
            "name": info.get("name", ""),
            "peg": fund["peg"], "track": fund["track"],
        }

        # 將 0-100 分數轉換為 multiplier/offset
        if score >= 80:
            result.buy_multiplier = 1.25
            result.sell_multiplier = 0.7
            result.buy_offset = 8.0
        elif score >= 65:
            result.buy_multiplier = 1.15
            result.sell_multiplier = 0.85
            result.buy_offset = 4.0
        elif score >= 45:
            result.buy_multiplier = 1.0
            result.sell_multiplier = 1.0
        elif score >= 30:
            result.buy_multiplier = 0.85
            result.sell_multiplier = 1.1
        else:
            result.buy_multiplier = 0.65
            result.sell_multiplier = 1.25
            result.sell_offset = 5.0

        result.reason = fund["advice"]
        result.details["valuation_action"] = fund["advice"]

        return result


# 註冊到 LayerRegistry
LayerRegistry.register("fundamental", FundamentalLayer)
