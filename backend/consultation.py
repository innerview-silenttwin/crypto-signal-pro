"""
投資諮詢系統

功能：
1. 根據用戶持倉（股票代碼、買入均價、持有張數）提供操作建議
2. 從歷史信號績效快取中，找出技術面/籌碼面/盤勢「類似情況」
3. 統計類似情況後 15 個交易日的前瞻報酬，推算加碼/持有/減碼/出清建議
4. 融合當前五維分析（籌碼、技術、基本面、盤勢）給出多維度理由

資料來源：
- 當前條件：screener 快取 or 即時掃描
- 歷史類似情況：signal_performance_cache.json（70+ 檔，2026-01起）
"""

import os
import sys
import json
import logging
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PERF_CACHE_FILE = os.path.join(DATA_DIR, "signal_performance_cache.json")
SCREENER_CACHE_FILE = os.path.join(DATA_DIR, "screener_cache.json")

REGIME_ADJACENT = {
    "強勢多頭": {"強勢多頭", "多頭"},
    "多頭":     {"強勢多頭", "多頭", "底部轉強"},
    "底部轉強": {"多頭", "底部轉強", "盤整"},
    "盤整":     {"底部轉強", "盤整", "高檔轉折"},
    "高檔轉折": {"盤整", "高檔轉折", "空頭"},
    "空頭":     {"高檔轉折", "空頭"},
}

REGIME_SCORE_MAP = {
    "強勢多頭": 90, "多頭": 75, "底部轉強": 70,
    "盤整": 50, "高檔轉折": 25, "空頭": 15,
}


# ── 快取載入 ─────────────────────────────────────────────────────

def _load_perf_cache() -> list:
    """載入信號績效快取（list of stock dicts）"""
    try:
        if os.path.exists(PERF_CACHE_FILE):
            with open(PERF_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            stocks = data.get("stocks", [])
            return stocks if isinstance(stocks, list) else []
    except Exception as e:
        logger.warning(f"載入績效快取失敗: {e}")
    return []


def _load_screener_cache() -> list:
    """載入選股快取（list of stock result dicts）"""
    try:
        if os.path.exists(SCREENER_CACHE_FILE):
            with open(SCREENER_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("results", [])
    except Exception as e:
        logger.warning(f"載入選股快取失敗: {e}")
    return []


# ── 取得股票當前狀態 ─────────────────────────────────────────────

def _get_current_conditions(symbol: str) -> Optional[dict]:
    """
    從選股快取中取得股票最新五維分數及詳情
    Returns None 若找不到
    """
    # 嘗試加 .TW 後綴
    sym_tw = symbol if symbol.endswith(".TW") else f"{symbol}.TW"
    results = _load_screener_cache()
    for r in results:
        if r.get("symbol") == sym_tw or r.get("symbol") == symbol:
            return r
    return None


def _get_current_price_from_cache(symbol: str) -> Optional[float]:
    """從績效快取中取得最新收盤價"""
    sym_tw = symbol if symbol.endswith(".TW") else f"{symbol}.TW"
    stocks = _load_perf_cache()
    for s in stocks:
        if s.get("symbol") == sym_tw or s.get("symbol") == symbol:
            daily = s.get("daily_scores", [])
            if daily:
                return daily[-1].get("close")
    return None


def _fetch_current_price_yfinance(symbol: str) -> Optional[float]:
    """即時從 yfinance 取最新收盤價（備用）"""
    try:
        import yfinance as yf
        sym_tw = symbol if symbol.endswith(".TW") else f"{symbol}.TW"
        ticker = yf.Ticker(sym_tw)
        hist = ticker.history(period="5d", interval="1d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"yfinance 取價失敗 {symbol}: {e}")
    return None


# ── 歷史類似情況比對 ──────────────────────────────────────────────

FORWARD_HORIZONS = {
    "short": 5,    # 短期：5 個交易日（約一週）
    "mid": 15,     # 中期：15 個交易日（約三週）
    "long": 30,    # 長期：30 個交易日（約六週）
}


def _find_similar_situations(
    current_tech: float,
    current_chip: float,
    current_regime: str,
    perf_stocks: list,
    target_symbol: str = "",
) -> list:
    """
    在歷史中找「類似情況」，同股票歷史優先

    相似條件（滿足 2/3 即算符合）：
    1. regime_state 在相鄰盤勢集合中
    2. tech_score 差距 ≤ 20
    3. chip_score 差距 ≤ 25

    同股票 is_self_match = True，排序時優先顯示

    回傳每筆 match 包含 short/mid/long 三種前瞻報酬
    """
    adjacent_regimes = REGIME_ADJACENT.get(current_regime, {current_regime})
    max_horizon = max(FORWARD_HORIZONS.values())
    matches = []

    for stock in perf_stocks:
        sym = stock.get("symbol", "")
        name = stock.get("name", "")
        daily = stock.get("daily_scores", [])
        n = len(daily)
        is_self = (sym == target_symbol)

        for i, day in enumerate(daily):
            if i + max_horizon >= n:
                break

            regime_match = day.get("regime_state", "") in adjacent_regimes
            tech_match = abs(day.get("tech", 50) - current_tech) <= 20
            chip_match = abs(day.get("chip", 50) - current_chip) <= 25

            if sum([regime_match, tech_match, chip_match]) < 2:
                continue

            close_now = day.get("close", 0)
            if close_now <= 0:
                continue

            # 計算三種期限的前瞻報酬
            forward_returns = {}
            for label, days in FORWARD_HORIZONS.items():
                if i + days < n:
                    fc = daily[i + days].get("close", 0)
                    forward_returns[label] = round((fc / close_now - 1) * 100, 2)
                else:
                    forward_returns[label] = None

            matches.append({
                "symbol": sym,
                "name": name,
                "date": day["date"],
                "close": close_now,
                "tech": day.get("tech", 50),
                "chip": day.get("chip", 50),
                "regime_state": day.get("regime_state", ""),
                "is_self": is_self,
                "forward_returns": forward_returns,  # {short, mid, long}
            })

    return matches


# ── 建議邏輯 ────────────────────────────────────────────────────

def _generate_recommendation(
    current_conditions: Optional[dict],
    current_regime: str,
    current_tech: float,
    current_chip: float,
    unrealized_pnl_pct: float,
    matches: list,
) -> dict:
    """
    根據當前情況 + 歷史類似情況統計，產出建議

    Returns:
        {recommendation, confidence, avg_forward_return, win_rate,
         reasoning, risk_factors}
    """
    reasoning = []
    risk_factors = []

    # ── 統計歷史類似情況（中期 15 日為主決策依據）──
    n_matches = len(matches)
    avg_ret = 0.0
    win_rate = 50.0
    if n_matches > 0:
        mid_rets = [m["forward_returns"].get("mid", 0) for m in matches
                    if m["forward_returns"].get("mid") is not None]
        if mid_rets:
            avg_ret = round(sum(mid_rets) / len(mid_rets), 2)
            win_rate = round(sum(1 for r in mid_rets if r > 0) / len(mid_rets) * 100, 1)

    # ── 評估各維度 ──
    scores = current_conditions.get("scores", {}) if current_conditions else {}
    raw_scores = current_conditions.get("raw_scores", {}) if current_conditions else {}

    raw_tech = raw_scores.get("technical", current_tech)
    raw_chip = raw_scores.get("chipflow", current_chip)

    chipflow = current_conditions.get("details", {}).get("chipflow", {}) if current_conditions else {}
    foreign_consec = chipflow.get("foreign_consec_buy", 0)
    trust_consec = chipflow.get("trust_consec_buy", 0)

    pe = current_conditions.get("details", {}).get("pe") if current_conditions else None
    peg = current_conditions.get("details", {}).get("peg") if current_conditions else None
    yoy = current_conditions.get("details", {}).get("yoy") if current_conditions else None
    fund_track = current_conditions.get("details", {}).get("fund_track", "value") if current_conditions else "value"

    tech_pillars = current_conditions.get("details", {}).get("tech_pillars", {}) if current_conditions else {}

    # ── 組合評分：主要信號 ──
    bullish_signals = 0
    bearish_signals = 0

    # 盤勢
    regime_score_val = REGIME_SCORE_MAP.get(current_regime, 50)
    if current_regime in ("強勢多頭", "多頭", "底部轉強"):
        bullish_signals += 1
        reasoning.append(f"盤勢「{current_regime}」—多頭環境有利持倉")
    elif current_regime in ("空頭",):
        bearish_signals += 2
        risk_factors.append(f"盤勢「{current_regime}」—空頭環境建議降低持倉")
    elif current_regime == "高檔轉折":
        bearish_signals += 1
        risk_factors.append(f"盤勢「高檔轉折」—趨勢可能反轉，注意停利")

    # 技術面
    if raw_tech >= 65:
        bullish_signals += 1
        reasoning.append(f"技術面強勢（原始分 {raw_tech:.0f}）：多指標共振偏多")
    elif raw_tech <= 35:
        bearish_signals += 1
        risk_factors.append(f"技術面偏弱（原始分 {raw_tech:.0f}）：指標轉空，注意下行風險")

    if tech_pillars.get("trend", 50) >= 65:
        bullish_signals += 1
        reasoning.append(f"趨勢柱面強（{tech_pillars['trend']:.0f}分）：EMA均線多排＋ADX確認趨勢")
    if tech_pillars.get("support", 50) >= 65:
        bullish_signals += 1
        reasoning.append(f"支撐柱面強（{tech_pillars['support']:.0f}分）：均線拉回支撐，逢低買點")

    # 籌碼面
    if foreign_consec >= 5:
        bullish_signals += 2
        reasoning.append(f"外資連買 {foreign_consec} 天，持續買超力道強，主動聚集中")
    elif foreign_consec >= 3:
        bullish_signals += 1
        reasoning.append(f"外資連買 {foreign_consec} 天，法人看好訊號")
    elif foreign_consec <= -3:
        bearish_signals += 1
        risk_factors.append(f"外資連賣，籌碼可能鬆動")

    if trust_consec >= 3:
        bullish_signals += 1
        reasoning.append(f"投信連買 {trust_consec} 天，基金有基本面支撐")

    if raw_chip >= 70:
        bullish_signals += 1
        reasoning.append(f"整體籌碼面強勢（原始分 {raw_chip:.0f}），法人積極布局")
    elif raw_chip <= 35:
        bearish_signals += 1
        risk_factors.append(f"籌碼面偏弱（原始分 {raw_chip:.0f}），散戶或法人賣壓")

    # 基本面
    if peg is not None and peg < 1.0:
        bullish_signals += 1
        reasoning.append(f"PEG={peg:.2f} < 1，成長股估值低估")
    elif pe is not None and pe < 12:
        bullish_signals += 1
        reasoning.append(f"本益比 {pe:.1f} 偏低，安全邊際高")
    elif pe is not None and pe > 30 and fund_track == "value":
        bearish_signals += 1
        risk_factors.append(f"本益比 {pe:.1f} 偏高，估值壓力需留意")

    if yoy is not None and yoy >= 20:
        bullish_signals += 1
        reasoning.append(f"營收 YoY +{yoy:.0f}%，成長動能強勁")
    elif yoy is not None and yoy <= -10:
        bearish_signals += 1
        risk_factors.append(f"營收 YoY {yoy:.0f}%，業績衰退中")

    # 損益狀況
    if unrealized_pnl_pct <= -15:
        bearish_signals += 1
        risk_factors.append(f"帳面虧損已達 {unrealized_pnl_pct:.1f}%，停損點接近")
    elif unrealized_pnl_pct >= 30:
        reasoning.append(f"已獲利 {unrealized_pnl_pct:.1f}%，可考慮部分停利保護")

    # 歷史類似情況統計
    if n_matches >= 5:
        desc = f"歷史找到 {n_matches} 個類似情況（盤勢/技術/籌碼相近）"
        if avg_ret > 0:
            desc += f"，15交易日平均報酬 +{avg_ret:.1f}%，勝率 {win_rate:.0f}%"
        else:
            desc += f"，15交易日平均報酬 {avg_ret:.1f}%，勝率 {win_rate:.0f}%"
        if avg_ret > 3:
            bullish_signals += 1
            reasoning.append(desc)
        elif avg_ret < -3:
            bearish_signals += 1
            risk_factors.append(desc)
        else:
            reasoning.append(desc + "，方向不明，謹慎操作")
    else:
        reasoning.append(f"歷史類似情況較少（{n_matches} 筆），建議以基本面為主要依據")

    # ── 最終建議邏輯 ──
    net_signal = bullish_signals - bearish_signals

    # 強力出清：連賣盤勢 or 大虧損
    if current_regime == "空頭" and unrealized_pnl_pct <= -10:
        recommendation = "出清"
        reasoning.insert(0, "空頭盤勢 + 持倉虧損，建議停損出清以控制風險")
    elif bearish_signals >= 3 and net_signal <= -2:
        recommendation = "出清"
        reasoning.insert(0, "多項負面指標共振，建議清倉")
    elif net_signal <= -1 or (avg_ret < -5 and n_matches >= 5):
        recommendation = "減碼"
        reasoning.insert(0, "負面信號偏多，建議先減倉至半，觀察後市")
    elif net_signal >= 3 and unrealized_pnl_pct > -20 and avg_ret > 5 and win_rate >= 60:
        recommendation = "加碼"
        reasoning.insert(0, "多方信號強烈，歷史勝率高，可考慮加碼")
    elif net_signal >= 2:
        recommendation = "加碼"
        reasoning.insert(0, "多方信號偏多，條件允許時可考慮加碼")
    else:
        recommendation = "持有"
        reasoning.insert(0, "當前多空力量相對均衡，維持持倉觀察")

    # 如果已大虧且信號中性，建議減碼
    if recommendation == "持有" and unrealized_pnl_pct <= -20:
        recommendation = "減碼"
        reasoning[0] = f"帳面虧損達 {unrealized_pnl_pct:.1f}%，即便信號中性仍建議降低部位"

    confidence = min(100, abs(net_signal) * 20 + (50 if n_matches >= 5 else 30))

    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "avg_forward_return": avg_ret,
        "win_rate": win_rate,
        "n_matches": n_matches,
        "reasoning": reasoning,
        "risk_factors": risk_factors,
    }


# ── 主要諮詢函數 ──────────────────────────────────────────────────

def consult_position(symbol: str, buy_price: float, quantity: int) -> dict:
    """
    持倉諮詢主函數

    Args:
        symbol:     股票代碼（如 "2317" 或 "2317.TW"）
        buy_price:  買入均價（元）
        quantity:   持有張數（1 張 = 1000 股）

    Returns:
        {
            symbol, name, current_price,
            cost_basis, current_value,
            unrealized_pnl, unrealized_pnl_pct,
            current_conditions: {tech, chip, regime_state, composite, highlights, ...},
            historical_analysis: {n_matches, avg_forward_return, win_rate, top_cases},
            recommendation, confidence,
            reasoning, risk_factors,
            data_source
        }
    """
    sym_tw = symbol if symbol.endswith(".TW") else f"{symbol}.TW"

    # ── 1. 取得股票名稱 ──
    from screener import SCREENER_UNIVERSE
    name = SCREENER_UNIVERSE.get(sym_tw, "")
    if not name:
        # 嘗試去掉 .TW 後在宇宙中找
        for k, v in SCREENER_UNIVERSE.items():
            if k.split(".")[0] == symbol.split(".")[0]:
                name = v
                sym_tw = k
                break

    # ── 2. 取得當前股價 ──
    current_price = _get_current_price_from_cache(sym_tw)
    data_source = "績效快取"
    if current_price is None:
        current_price = _fetch_current_price_yfinance(sym_tw)
        data_source = "即時報價(yfinance)"
    if current_price is None:
        current_price = buy_price  # fallback：用買入價
        data_source = "無最新報價，以買入價代替"

    # ── 3. 損益計算 ──
    shares = quantity * 1000  # 1 張 = 1000 股
    cost_basis = buy_price * shares
    current_value = current_price * shares
    unrealized_pnl = current_value - cost_basis
    unrealized_pnl_pct = (current_price / buy_price - 1) * 100 if buy_price > 0 else 0.0

    # ── 4. 取得當前五維條件 ──
    current_cond = _get_current_conditions(sym_tw)
    current_tech = 50.0
    current_chip = 50.0
    current_regime = "盤整"

    if current_cond:
        raw = current_cond.get("raw_scores", {})
        scores = current_cond.get("scores", {})
        current_tech = raw.get("technical", scores.get("technical", 50.0))
        current_chip = raw.get("chipflow", scores.get("chipflow", 50.0))
        current_regime = current_cond.get("details", {}).get("regime_state", "盤整")
    else:
        # 快取沒有 → 從績效快取最新一天推斷
        perf_stocks = _load_perf_cache()
        for s in perf_stocks:
            if s.get("symbol") == sym_tw:
                daily = s.get("daily_scores", [])
                if daily:
                    last = daily[-1]
                    current_tech = last.get("tech", 50.0)
                    current_chip = last.get("chip", 50.0)
                    current_regime = last.get("regime_state", "盤整")
                break

    # ── 5. 歷史類似情況比對 ──
    perf_stocks = _load_perf_cache()
    matches = _find_similar_situations(
        current_tech=current_tech,
        current_chip=current_chip,
        current_regime=current_regime,
        perf_stocks=perf_stocks,
        target_symbol=sym_tw,
    )

    # 分為同股票 / 其他股票
    self_matches = [m for m in matches if m["is_self"]]
    other_matches = [m for m in matches if not m["is_self"]]

    # ── 統計短中長期報酬 ──
    def _compute_horizon_stats(match_list):
        stats = {}
        for label in FORWARD_HORIZONS.keys():
            rets = [m["forward_returns"][label] for m in match_list
                    if m["forward_returns"].get(label) is not None]
            if rets:
                stats[label] = {
                    "avg_return": round(sum(rets) / len(rets), 2),
                    "win_rate": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
                    "count": len(rets),
                    "best": round(max(rets), 2),
                    "worst": round(min(rets), 2),
                }
            else:
                stats[label] = {"avg_return": 0, "win_rate": 50, "count": 0, "best": 0, "worst": 0}
        return stats

    self_horizon_stats = _compute_horizon_stats(self_matches)
    other_horizon_stats = _compute_horizon_stats(other_matches)
    all_horizon_stats = _compute_horizon_stats(matches)

    # 代表性案例：同股票優先排前，再補其他股票
    top_cases = []
    # 同股票案例（最多 5 筆，正負各選）
    self_pos = sorted([m for m in self_matches if (m["forward_returns"].get("mid") or 0) > 0],
                      key=lambda x: -(x["forward_returns"].get("mid") or 0))[:3]
    self_neg = sorted([m for m in self_matches if (m["forward_returns"].get("mid") or 0) < 0],
                      key=lambda x: (x["forward_returns"].get("mid") or 0))[:2]
    for m in self_pos + self_neg:
        top_cases.append({
            "symbol": m["symbol"], "name": m["name"], "date": m["date"],
            "regime": m["regime_state"], "tech": m["tech"], "chip": m["chip"],
            "is_self": True,
            "forward_returns": m["forward_returns"],
        })

    # 其他股票案例（補滿到 8 筆）
    remaining = 8 - len(top_cases)
    if remaining > 0:
        other_pos = sorted([m for m in other_matches if (m["forward_returns"].get("mid") or 0) > 0],
                           key=lambda x: -(x["forward_returns"].get("mid") or 0))[:max(2, remaining // 2)]
        other_neg = sorted([m for m in other_matches if (m["forward_returns"].get("mid") or 0) < 0],
                           key=lambda x: (x["forward_returns"].get("mid") or 0))[:remaining - len(other_pos)]
        for m in (other_pos + other_neg)[:remaining]:
            top_cases.append({
                "symbol": m["symbol"], "name": m["name"], "date": m["date"],
                "regime": m["regime_state"], "tech": m["tech"], "chip": m["chip"],
                "is_self": False,
                "forward_returns": m["forward_returns"],
            })

    # ── 6. 生成建議 ──
    result = _generate_recommendation(
        current_conditions=current_cond,
        current_regime=current_regime,
        current_tech=current_tech,
        current_chip=current_chip,
        unrealized_pnl_pct=unrealized_pnl_pct,
        matches=matches,
    )

    # ── 7. 組裝回傳 ──
    conditions_summary = {
        "tech_score": round(current_tech, 1),
        "chip_score": round(current_chip, 1),
        "regime_state": current_regime,
        "regime_score": REGIME_SCORE_MAP.get(current_regime, 50),
        "composite": current_cond.get("composite") if current_cond else None,
        "highlights": current_cond.get("highlights", []) if current_cond else [],
        "tech_pillars": current_cond.get("details", {}).get("tech_pillars", {}) if current_cond else {},
        "foreign_consec_buy": current_cond.get("details", {}).get("chipflow", {}).get("foreign_consec_buy", 0) if current_cond else 0,
        "trust_consec_buy": current_cond.get("details", {}).get("chipflow", {}).get("trust_consec_buy", 0) if current_cond else 0,
        "pe": current_cond.get("details", {}).get("pe") if current_cond else None,
        "peg": current_cond.get("details", {}).get("peg") if current_cond else None,
        "yoy": current_cond.get("details", {}).get("yoy") if current_cond else None,
    }

    horizon_labels = {"short": "短期(5日)", "mid": "中期(15日)", "long": "長期(30日)"}

    return {
        "symbol": sym_tw,
        "name": name or symbol,
        "current_price": round(current_price, 2),
        "buy_price": round(buy_price, 2),
        "quantity": quantity,
        "cost_basis": round(cost_basis),
        "current_value": round(current_value),
        "unrealized_pnl": round(unrealized_pnl),
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
        "current_conditions": conditions_summary,
        "historical_analysis": {
            "self_matches": len(self_matches),
            "other_matches": len(other_matches),
            "total_matches": len(matches),
            "self_horizon_stats": self_horizon_stats,
            "other_horizon_stats": other_horizon_stats,
            "all_horizon_stats": all_horizon_stats,
            "horizon_labels": horizon_labels,
            "top_cases": top_cases,
        },
        "recommendation": result["recommendation"],
        "confidence": result["confidence"],
        "reasoning": result["reasoning"],
        "risk_factors": result["risk_factors"],
        "data_source": data_source,
    }
