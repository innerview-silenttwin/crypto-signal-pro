"""
超級選股引擎 (Super Stock Screener)

功能：
1. 批次掃描台股 universe，計算五維度分數
2. 依條件分類為五大精選類別
3. 快取結果，供 API 端點讀取

五大精選類別：
- 外資狂買股：外資連買 >= 3天
- 投信認養股：投信連買 >= 3天
- 籌碼集中股：融資減少 + 法人買超
- 價值低估股：PE < 12 + 基本面分數 >= 70
- 技術突破股：盤勢=強勢多頭/底部轉強 + 技術分數 >= 70
"""

import os
import sys
import json
import time
import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals.aggregator import SignalAggregator
from layers.fundamental import fetch_twse_pe_all, _strip_tw
from layers.chipflow import fetch_chip_summary, compute_chip_score
from layers.regime import RegimeLayer
from layers.sentiment import get_stock_sentiment, get_market_sentiment, fetch_rss_articles

logger = logging.getLogger(__name__)

# ── 各產業最佳技術指標權重（回測驗證） ──
# 來源：sector_backtest_report.txt (2019-2026, 7 年回測)
SECTOR_WEIGHTS = {
    "semiconductor": {  # 半導體：趨勢追蹤 EMA+ADX（標準）
        'rsi': 10.0, 'macd': 15.0, 'bollinger': 5.0,
        'mfi': 5.0, 'ema_cross': 30.0, 'volume': 10.0, 'adx': 25.0,
    },
    "electronics": {  # 電子：趨勢追蹤 EMA+ADX（寬鬆）
        'rsi': 10.0, 'macd': 15.0, 'bollinger': 5.0,
        'mfi': 5.0, 'ema_cross': 30.0, 'volume': 10.0, 'adx': 25.0,
    },
    "finance": {  # 金融：動能+趨勢 RSI+MACD+EMA（標準）
        'rsi': 20.0, 'macd': 25.0, 'bollinger': 5.0,
        'mfi': 5.0, 'ema_cross': 25.0, 'volume': 10.0, 'adx': 10.0,
    },
    "traditional": {  # 傳產：趨勢追蹤 EMA+ADX（寬鬆）+ Regime Veto-Only
        'rsi': 10.0, 'macd': 15.0, 'bollinger': 5.0,
        'mfi': 5.0, 'ema_cross': 30.0, 'volume': 10.0, 'adx': 25.0,
    },
    "default": {  # 其他（生技、ETF 等）：通用台股權重
        'rsi': 10.0, 'macd': 15.0, 'bollinger': 10.0,
        'mfi': 15.0, 'ema_cross': 15.0, 'volume': 25.0, 'adx': 10.0,
    },
}

# ── 各產業綜合分數五維權重 ──
# 依據：Regime 回測結果 + 產業特性推理
SECTOR_COMPOSITE_WEIGHTS = {
    "semiconductor": {  # 法人主導、趨勢明確、regime 回測夏普+0.89
        "chipflow": 0.35, "technical": 0.25, "fundamental": 0.15,
        "regime": 0.18, "sentiment": 0.07,
    },
    "electronics": {  # 同半導體，regime 回測效果最強（夏普+0.94）
        "chipflow": 0.35, "technical": 0.25, "fundamental": 0.15,
        "regime": 0.18, "sentiment": 0.07,
    },
    "finance": {  # 殖利率重要、波動小；籌碼回測有害(夏普-0.19)，降權
        "chipflow": 0.15, "technical": 0.20, "fundamental": 0.38,
        "regime": 0.13, "sentiment": 0.14,
    },
    "traditional": {  # regime 回測有害（夏普-0.36），基本面對景氣循環股重要
        "chipflow": 0.30, "technical": 0.25, "fundamental": 0.30,
        "regime": 0.05, "sentiment": 0.10,
    },
    "default": {  # 通用
        "chipflow": 0.35, "fundamental": 0.20, "technical": 0.25,
        "regime": 0.13, "sentiment": 0.07,
    },
}

# ── 股票代碼 → 產業分類 ──
SYMBOL_SECTOR_MAP = {}

# 半導體
for _s in ["2330.TW", "2454.TW", "2303.TW", "3711.TW", "2379.TW", "3034.TW",
           "6415.TW", "2344.TW", "3529.TW", "5274.TW", "2408.TW", "6770.TW"]:
    SYMBOL_SECTOR_MAP[_s] = "semiconductor"

# 電子代工 / AI / 零組件
for _s in ["2317.TW", "2382.TW", "2308.TW", "2357.TW", "3008.TW", "2345.TW",
           "3231.TW", "2356.TW", "4938.TW", "3443.TW", "2395.TW", "6669.TW",
           "3037.TW", "2327.TW", "3661.TW", "2376.TW", "3017.TW", "2353.TW",
           "6488.TW", "2301.TW", "2474.TW", "8046.TW", "3653.TW"]:
    SYMBOL_SECTOR_MAP[_s] = "electronics"

# 金融
for _s in ["2881.TW", "2882.TW", "2891.TW", "2886.TW", "2884.TW", "2880.TW",
           "2887.TW", "2890.TW", "2883.TW", "2892.TW", "5880.TW", "2885.TW"]:
    SYMBOL_SECTOR_MAP[_s] = "finance"

# 傳產 / 航運 / 鋼鐵 / 塑化 / 電信 / 食品
for _s in ["1301.TW", "2002.TW", "1216.TW", "2603.TW", "2609.TW", "2615.TW",
           "1303.TW", "1326.TW", "1101.TW", "2207.TW", "9910.TW",
           "2412.TW", "3045.TW", "4904.TW", "2912.TW", "1590.TW",
           "2049.TW", "1513.TW", "6505.TW", "2618.TW"]:
    SYMBOL_SECTOR_MAP[_s] = "traditional"


def get_sector_weights(symbol: str) -> dict:
    """取得股票對應的產業最佳技術面權重"""
    sector = SYMBOL_SECTOR_MAP.get(symbol, "default")
    return SECTOR_WEIGHTS[sector]


def get_symbol_sector(symbol: str) -> str:
    """取得股票所屬產業 ID"""
    return SYMBOL_SECTOR_MAP.get(symbol, "default")


# ── 選股宇宙（約 100 檔台股權值+熱門股） ──

SCREENER_UNIVERSE = {
    # 半導體
    "2330.TW": "台積電", "2454.TW": "聯發科", "2303.TW": "聯電",
    "3711.TW": "日月光投控", "2379.TW": "瑞昱", "3034.TW": "聯詠",
    "6415.TW": "矽力-KY", "2344.TW": "華邦電", "3529.TW": "力旺",
    "5274.TW": "信驊",
    # 電子代工 / AI / 零組件
    "2317.TW": "鴻海", "2382.TW": "廣達", "2308.TW": "台達電",
    "2357.TW": "華碩", "3008.TW": "大立光", "2345.TW": "智邦",
    "3231.TW": "緯創", "2356.TW": "英業達", "4938.TW": "和碩",
    "3443.TW": "創意", "2395.TW": "研華", "6669.TW": "緯穎",
    "3037.TW": "欣興", "2327.TW": "國巨", "3661.TW": "世芯-KY",
    "2376.TW": "技嘉", "3017.TW": "奇鋐", "2353.TW": "宏碁",
    "6488.TW": "環球晶",
    # 金融
    "2881.TW": "富邦金", "2882.TW": "國泰金", "2891.TW": "中信金",
    "2886.TW": "兆豐金", "2884.TW": "玉山金", "2880.TW": "華南金",
    "2887.TW": "台新金", "2890.TW": "永豐金", "2883.TW": "開發金",
    "2892.TW": "第一金", "5880.TW": "合庫金", "2885.TW": "元大金",
    # 傳產 / 航運 / 鋼鐵 / 塑化
    "1301.TW": "台塑", "2002.TW": "中鋼", "1216.TW": "統一",
    "2603.TW": "長榮", "2609.TW": "陽明", "2615.TW": "萬海",
    "1303.TW": "南亞", "1326.TW": "台化", "1101.TW": "台泥",
    "2207.TW": "和泰車", "9910.TW": "豐泰",
    # 電信 / 公用
    "2412.TW": "中華電", "3045.TW": "台灣大", "4904.TW": "遠傳",
    # 生技
    "4743.TW": "合一", "6446.TW": "藥華藥", "1760.TW": "寶齡富錦",
    # 食品 / 零售
    "2912.TW": "統一超", "1590.TW": "亞德客-KY",
    # ETF (不需基本面)
    "0050.TW": "元大台灣50", "0056.TW": "元大高股息",
    "00878.TW": "國泰永續高股息", "00919.TW": "群益台灣精選高息",
    # 其他熱門
    "2301.TW": "光寶科", "2474.TW": "可成", "8046.TW": "南電",
    "2408.TW": "南亞科", "3653.TW": "健策", "6770.TW": "力積電",
    "2049.TW": "上銀", "1513.TW": "中興電", "6505.TW": "台塑化",
    "2618.TW": "長榮航",
}

# ── 快取 ──

_screener_cache: Dict = {}  # {"results": [...], "categories": [...], "updated_at": str}
SCREENER_CACHE_TTL = 3600 * 6  # 6 小時
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "screener_cache.json")


def _fetch_signal_data_for_screener(symbol: str) -> Optional[pd.DataFrame]:
    """用 yfinance 取得個股 OHLCV（for 技術面 + 盤勢分析）"""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="200d", interval="1d")
        if df.empty or len(df) < 60:
            return None
        df.columns = [c.lower() for c in df.columns]
        df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
        return df
    except Exception as e:
        logger.warning(f"yfinance 取得 {symbol} 失敗: {e}")
        return None


def scan_single_stock(symbol: str, name: str, all_pe: dict, articles: list) -> Optional[dict]:
    """
    掃描單一股票，計算五維度分數

    Returns:
        {symbol, name, scores: {technical, fundamental, chipflow, regime, sentiment},
         composite, highlights: [...], details: {...}}
    """
    try:
        scores = {}
        details = {}

        # ── 1. 基本面 ──
        code = _strip_tw(symbol)
        pe_info = all_pe.get(code)
        fund_score = 50
        pe = None
        
        if pe_info and pe_info.get("pe") and pe_info["pe"] > 0:
            pe = pe_info["pe"]
            dy = pe_info.get("dy")
            details["pe"] = pe
            details["dy"] = dy
            
            from layers.fundamental import fetch_twse_revenue_all, get_sector_pe_stats
            all_rev = fetch_twse_revenue_all()
            rev_info = all_rev.get(code, {})
            sector = rev_info.get("sector")
            details["sector"] = sector
            
            same_sector_symbols = []
            if sector:
                same_sector_symbols = [f"{c}.TW" for c, v in all_rev.items() if v.get("sector") == sector]
                
            pe_percentile = None
            if same_sector_symbols and len(same_sector_symbols) >= 3:
                pe_stats = get_sector_pe_stats(same_sector_symbols, all_pe)
                sym_key = f"{code}.TW"
                if sym_key in pe_stats:
                    pe_percentile = pe_stats[sym_key].get("percentile")
                    details["pe_percentile"] = pe_percentile
                    
            if pe_percentile is not None:
                if pe_percentile <= 20: fund_score = 90
                elif pe_percentile <= 40: fund_score = 75
                elif pe_percentile <= 60: fund_score = 55
                elif pe_percentile <= 80: fund_score = 30
                else: fund_score = 15
            else:
                if pe < 8: fund_score = 90
                elif pe < 12: fund_score = 75
                elif pe < 20: fund_score = 55
                elif pe < 30: fund_score = 30
                else: fund_score = 15
            
            # 殖利率加分
            if dy and dy >= 5.0:
                fund_score = min(100, fund_score + 10)
            elif dy and dy >= 3.0:
                fund_score = min(100, fund_score + 5)

            # 營收動能加分（與 stock-analysis 一致）
            yoy = rev_info.get("yoy") if rev_info else None
            if yoy is not None:
                if yoy > 20:
                    fund_score = min(100, fund_score + 10)
                elif yoy > 0:
                    fund_score = min(100, fund_score + 5)
                elif yoy < -20:
                    fund_score = max(0, fund_score - 10)

        scores["fundamental"] = fund_score

        # ── 2. 籌碼面 ──
        chip_summary = fetch_chip_summary(symbol, days=10)
        chip_score = 50
        if chip_summary:
            chip_result = compute_chip_score(chip_summary)
            chip_score = chip_result["score"]
            details["chipflow"] = {
                "label": chip_result["label"],
                "foreign_consec_buy": chip_summary.get("foreign_consec_buy", 0),
                "trust_consec_buy": chip_summary.get("trust_consec_buy", 0),
                "foreign_total_net": chip_summary.get("foreign_total_net", 0),
                "trust_total_net": chip_summary.get("trust_total_net", 0),
                "margin_change_sum": chip_summary.get("margin_change_sum", 0),
            }
        scores["chipflow"] = chip_score

        # ── 3. 技術面 + 盤勢（按產業使用回測最佳權重）──
        tech_score = 50
        regime_score = 50
        regime_state = "未知"
        symbol_sector = get_symbol_sector(symbol)
        df = _fetch_signal_data_for_screener(symbol)
        if df is not None and len(df) >= 120:
            # 技術面：使用該產業的回測最佳權重
            sector_w = get_sector_weights(symbol)
            agg = SignalAggregator(weights=sector_w)
            signal = agg.analyze(df.copy(), symbol, "1d")
            tech_score = round(float(signal.buy_score), 1)

            # 盤勢
            regime_layer = RegimeLayer(enabled=True)
            modifier = regime_layer.compute_modifier(symbol, df)
            regime_state = modifier.regime or "未知"
            regime_scores_map = {
                "強勢多頭": 90, "多頭": 75, "底部轉強": 70,
                "盤整": 50, "高檔轉折": 25, "空頭": 15,
            }
            regime_score = regime_scores_map.get(regime_state, 50)
            details["regime_state"] = regime_state

            # 傳產 Regime Veto-Only：只用空頭否決，不用多頭加乘
            # 回測顯示傳產的 Regime 多頭加乘反而有害（航運暴漲暴跌）
            if symbol_sector == "traditional" and regime_state in ("強勢多頭", "多頭"):
                regime_score = min(regime_score, 60)  # 限制多頭加分上限

        scores["technical"] = tech_score
        scores["regime"] = regime_score
        details["sector_type"] = symbol_sector

        # ── 4. 消息面（無相關新聞時設為 None，不列入綜合評分）──
        sent_score = None
        try:
            stock_name = name or (pe_info.get("name", "") if pe_info else "")
            sentiment = get_stock_sentiment(symbol, stock_name, articles)
            if sentiment["total_related"] > 0:
                raw_sent = sentiment["score"]
                sent_score = round(max(0, min(100, 50 + raw_sent * 0.5)), 1)
        except Exception:
            pass
        scores["sentiment"] = sent_score

        # ── 5. 綜合分數（按產業使用不同五維權重）──
        weights = SECTOR_COMPOSITE_WEIGHTS.get(symbol_sector, SECTOR_COMPOSITE_WEIGHTS["default"])
        # 跳過缺失的維度，重新分配權重（與 stock-analysis 邏輯一致）
        valid = [(scores.get(k, 50), w) for k, w in weights.items() if scores.get(k) is not None]
        if not valid:
            valid = [(50, 1.0)]
        total_w = sum(w for _, w in valid)
        composite = sum(s * w for s, w in valid) / total_w
        composite = round(composite, 1)

        # ── 6. 亮點文字 ──
        highlights = []
        fc = details.get("chipflow", {}).get("foreign_consec_buy", 0)
        tc = details.get("chipflow", {}).get("trust_consec_buy", 0)
        ft = details.get("chipflow", {}).get("foreign_total_net", 0)
        if fc >= 3:
            highlights.append(f"外資連買{fc}天")
        if tc >= 3:
            highlights.append(f"投信連買{tc}天")
        if details.get("pe_percentile") is not None and details["pe_percentile"] <= 40:
            highlights.append(f"產業低本益比(擊敗{100-details['pe_percentile']}%同業)")
        elif details.get("pe") and details["pe"] < 12:
            highlights.append(f"低本益比{details['pe']:.1f}")
        if regime_state in ("強勢多頭", "底部轉強"):
            highlights.append(f"盤勢{regime_state}")

        return {
            "symbol": symbol,
            "name": name,
            "scores": scores,
            "composite": composite,
            "highlights": highlights,
            "details": details,
        }

    except Exception as e:
        logger.warning(f"掃描 {symbol} 失敗: {e}")
        return None


def scan_all_stocks() -> List[dict]:
    """
    批次掃描所有選股宇宙，回傳每檔股票的五維度分數

    使用 ThreadPoolExecutor 並行取得 yfinance 資料
    """
    logger.info(f"開始掃描選股宇宙: {len(SCREENER_UNIVERSE)} 檔")
    start_time = time.time()

    # 先批次取回全市場資料（這些 API 一次取全部，不會因為 N 檔股票多呼叫）
    all_pe = fetch_twse_pe_all()
    try:
        articles = fetch_rss_articles()
    except Exception:
        articles = []

    # 預抓融資融券 OpenAPI 最新資料（三大法人改用 FinMind 個股查詢，不需全市場預抓）
    from layers.chipflow import _ensure_margin_openapi
    _ensure_margin_openapi()
    logger.info("融資融券 OpenAPI 預抓完成")

    results = []

    # 用 ThreadPoolExecutor 並行掃描（每檔主要耗時在 yfinance，籌碼面已全部快取）
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for symbol, name in SCREENER_UNIVERSE.items():
            future = executor.submit(scan_single_stock, symbol, name, all_pe, articles)
            futures[future] = symbol

        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result = future.result(timeout=30)
                if result:
                    results.append(result)
            except Exception as e:
                logger.warning(f"掃描 {symbol} timeout/error: {e}")

    # 依綜合分數排序
    results.sort(key=lambda x: x["composite"], reverse=True)

    elapsed = time.time() - start_time
    logger.info(f"選股掃描完成: {len(results)} 檔，耗時 {elapsed:.1f}秒")

    return results


def categorize_picks(results: List[dict]) -> List[dict]:
    """
    從掃描結果中篩選五大精選類別

    每類最多 5 檔，依綜合分數排序
    """
    categories = []

    # ── 0. 綜合排行榜（依總分排序前 15 名）──
    top_ranked = results[:15]  # results 已按 composite 降序排列
    for r in top_ranked:
        scores = r.get("scores", {})
        best_dim = max(scores, key=lambda k: scores.get(k) or 0) if scores else ""
        dim_names = {"chipflow": "籌碼", "fundamental": "基本面", "technical": "技術",
                     "regime": "盤勢", "sentiment": "消息"}
        best_name = dim_names.get(best_dim, best_dim)
        best_val = round(scores.get(best_dim, 0))
        r["_highlight"] = f"綜合{round(r['composite'])}分｜{best_name}{best_val}分最強"
    categories.append({
        "id": "top_ranked",
        "name": "綜合排行榜",
        "icon": "👑",
        "description": "綜合分數 = 籌碼×權重 + 技術×權重 + 基本面×權重 + 盤勢×權重 + 消息×權重。各產業權重不同，例：半導體/電子 籌碼35%，金融 基本面38% 為主。排名依綜合分數由高到低。",
        "stocks": _format_picks(top_ranked),
    })

    # ── 1. 外資狂買股 ──
    foreign_picks = []
    for r in results:
        fc = r.get("details", {}).get("chipflow", {}).get("foreign_consec_buy", 0)
        ft = r.get("details", {}).get("chipflow", {}).get("foreign_total_net", 0)
        if fc >= 3:
            r["_highlight"] = f"外資連買{fc}天，累計{_format_shares(ft)}"
            foreign_picks.append(r)
    foreign_picks.sort(key=lambda x: x["composite"], reverse=True)
    categories.append({
        "id": "foreign_buy",
        "name": "外資狂買股",
        "icon": "🏦",
        "description": "篩選條件：外資連續買超 ≥ 3 天。外資為台股最大買方，連續買超代表中長線看好，搭配大量買超金額更具參考價值。",
        "stocks": _format_picks(foreign_picks[:10]),
    })

    # ── 2. 投信認養股 ──
    trust_picks = []
    for r in results:
        tc = r.get("details", {}).get("chipflow", {}).get("trust_consec_buy", 0)
        tt = r.get("details", {}).get("chipflow", {}).get("trust_total_net", 0)
        if tc >= 3:
            r["_highlight"] = f"投信連買{tc}天，累計{_format_shares(tt)}"
            trust_picks.append(r)
    trust_picks.sort(key=lambda x: x["composite"], reverse=True)
    categories.append({
        "id": "trust_buy",
        "name": "投信認養股",
        "icon": "🎯",
        "description": "篩選條件：投信連續買超 ≥ 3 天。投信選股嚴謹，連續買超往往代表有基本面研究支撐，是中期波段的領先指標。",
        "stocks": _format_picks(trust_picks[:10]),
    })

    # ── 3. 籌碼集中股 ──
    chip_concentrated = []
    for r in results:
        chip = r.get("details", {}).get("chipflow", {})
        mc = chip.get("margin_change_sum", 0)
        fc = chip.get("foreign_consec_buy", 0)
        tc = chip.get("trust_consec_buy", 0)
        # 融資減少 + 法人買超
        if mc < -500 and (fc > 0 or tc > 0):
            r["_highlight"] = f"融資減{abs(mc)}張＋法人買超"
            chip_concentrated.append(r)
    chip_concentrated.sort(key=lambda x: x["composite"], reverse=True)
    categories.append({
        "id": "chip_concentrated",
        "name": "籌碼集中股",
        "icon": "🔒",
        "description": "篩選條件：融資餘額減少 > 500 張，且外資或投信同步買超。融資減少代表散戶離場、籌碼沉澱到法人手中，是股價醞釀上漲的前兆。",
        "stocks": _format_picks(chip_concentrated[:10]),
    })

    # ── 4. 價值低估股 ──
    value_picks = []
    for r in results:
        pe = r.get("details", {}).get("pe")
        pe_percentile = r.get("details", {}).get("pe_percentile")
        fund_score = r.get("scores", {}).get("fundamental", 0)
        
        # 條件：傳統本益比小於 12，或是產業估值前 30% 低估，且基本面達 70 分
        if fund_score >= 70 and ((pe and pe < 12) or (pe_percentile and pe_percentile <= 30)):
            if pe_percentile and pe_percentile <= 30:
                r["_highlight"] = f"產業低估前{pe_percentile}%，基本面{fund_score}分"
            else:
                r["_highlight"] = f"P/E {pe:.1f}，基本面{fund_score}分"
            value_picks.append(r)
    value_picks.sort(key=lambda x: x["composite"], reverse=True)
    categories.append({
        "id": "value_underpriced",
        "name": "價值低估股",
        "icon": "💎",
        "description": "篩選條件：本益比 < 12 或產業估值前 30% 低估，且基本面分數 ≥ 70。基本面分數綜合本益比、殖利率、營收年增率計算。",
        "stocks": _format_picks(value_picks[:10]),
    })

    # ── 5. 技術突破股 ──
    tech_picks = []
    for r in results:
        regime = r.get("details", {}).get("regime_state", "")
        tech_score = r.get("scores", {}).get("technical", 0)
        if regime in ("強勢多頭", "底部轉強") and tech_score >= 70:
            r["_highlight"] = f"盤勢{regime}＋技術{tech_score}分"
            tech_picks.append(r)
    tech_picks.sort(key=lambda x: x["composite"], reverse=True)
    categories.append({
        "id": "tech_breakout",
        "name": "技術突破股",
        "icon": "🚀",
        "description": "篩選條件：盤勢狀態為「強勢多頭」或「底部轉強」，且技術面分數 ≥ 70。技術分數綜合 EMA 趨勢、ADX 動能、MACD、RSI 等指標，各產業權重不同。",
        "stocks": _format_picks(tech_picks[:10]),
    })

    return categories


def _format_shares(shares: int) -> str:
    """格式化張數顯示"""
    if abs(shares) >= 10000:
        return f"{shares/10000:.1f}萬張"
    elif abs(shares) >= 1000:
        return f"{shares/1000:.1f}千張"
    else:
        return f"{shares}張"


def _format_picks(picks: List[dict]) -> List[dict]:
    """格式化精選股票為前端需要的格式"""
    formatted = []
    for p in picks:
        formatted.append({
            "symbol": p["symbol"],
            "name": p["name"],
            "composite_score": p["composite"],
            "highlight": p.get("_highlight", ""),
            "scores": p["scores"],
        })
    return formatted


def clear_cache():
    """清除記憶體快取 + 檔案快取"""
    global _screener_cache
    _screener_cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            os.remove(CACHE_FILE)
            logger.info("選股快取已清除")
        except Exception as e:
            logger.warning(f"清除快取失敗: {e}")


def get_screener_results() -> dict:
    """
    取得選股結果（優先從快取讀取）

    Returns:
        {"results": [...], "categories": [...], "updated_at": str, "total": int}
    """
    global _screener_cache

    now = time.time()

    # 記憶體快取
    if _screener_cache and now - _screener_cache.get("time", 0) < SCREENER_CACHE_TTL:
        return _screener_cache.get("data", {})

    # 檔案快取
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cache_time = cached.get("time", 0)
            if now - cache_time < SCREENER_CACHE_TTL:
                _screener_cache = {"data": cached, "time": cache_time}
                return cached
        except Exception:
            pass

    # 都沒快取 → 回傳空，背景觸發掃描
    return {"results": [], "categories": [], "updated_at": "", "total": 0, "status": "no_cache"}


def run_screener_scan() -> dict:
    """
    執行完整掃描並儲存快取

    Returns:
        {"results": [...], "categories": [...], "updated_at": str, "total": int}
    """
    global _screener_cache

    results = scan_all_stocks()
    categories = categorize_picks(results)

    # 只保留前 50 筆到結果中（避免 JSON 太大）
    top_results = []
    for r in results[:50]:
        top_results.append({
            "symbol": r["symbol"],
            "name": r["name"],
            "composite": r["composite"],
            "scores": r["scores"],
            "highlights": r["highlights"],
        })

    data = {
        "results": top_results,
        "categories": categories,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total": len(results),
        "time": time.time(),
    }

    # 存檔案快取
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"選股結果已存檔: {CACHE_FILE}")
    except Exception as e:
        logger.warning(f"選股結果存檔失敗: {e}")

    # 記憶體快取
    _screener_cache = {"data": data, "time": time.time()}

    return data


# ── 背景排程 ──

_scan_thread: Optional[threading.Thread] = None
_scan_lock = threading.Lock()


def trigger_background_scan():
    """在背景執行選股掃描（非阻塞）"""
    global _scan_thread

    with _scan_lock:
        if _scan_thread and _scan_thread.is_alive():
            logger.info("選股掃描已在執行中，跳過")
            return False

        _scan_thread = threading.Thread(target=run_screener_scan, daemon=True)
        _scan_thread.start()
        logger.info("背景選股掃描已啟動")
        return True


def is_scanning() -> bool:
    """是否正在掃描中"""
    return _scan_thread is not None and _scan_thread.is_alive()
