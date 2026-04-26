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
- 技術突破股：盤勢=強勢多頭/底部轉強 + 原始技術分數 >= 55
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
from layers.sentiment import get_stock_sentiment, fetch_rss_articles
from layers.active_etf import get_active_etf_score

logger = logging.getLogger(__name__)

# ── 各產業最佳技術指標權重（回測驗證） ──
# 來源：chipflow_backtest_20260410 指標歸因分析（D模式，2019-2026 7年）
# 指標集：RSI、MACD、Bollinger、MFI、EMA Cross、Volume、ADX
#         + StochRSI（拉回超賣）、VolumeReversal（爆量反轉/破底警示）、PullbackSupport（均線拉回+破底保護）
# 調整原則：Bollinger/MFI 全產業負貢獻 → 降至 2；釋出權重補回各產業歸因最強指標
SECTOR_WEIGHTS = {
    # 歸因依據：chipflow_backtest_20260410 D模式分析
    # Bollinger/MFI 在所有產業均負貢獻 → 全面降至 2
    # 釋出的權重回補至各產業歸因最強指標
    "semiconductor": {
        # 歸因最強：MACD +12.2%勝率提升、ADX +3.15%報酬提升
        # bollinger:5→2(-3), mfi:5→2(-3) 釋出6，補到 macd(+3)、adx(+3)
        'rsi': 10.0, 'macd': 18.0, 'bollinger': 2.0,
        'mfi': 2.0, 'ema_cross': 30.0, 'volume': 10.0, 'adx': 28.0,
        'stoch_rsi': 6.0, 'volume_reversal': 12.0, 'pullback_support': 10.0,
    },
    "electronics": {
        # 歸因最強：Pullback Support +4.8%勝率提升、ADX +1.59%報酬提升
        # bollinger:5→2(-3), mfi:5→2(-3) 釋出6，補到 pullback(+4)、adx(+2)
        'rsi': 10.0, 'macd': 15.0, 'bollinger': 2.0,
        'mfi': 2.0, 'ema_cross': 30.0, 'volume': 10.0, 'adx': 27.0,
        'stoch_rsi': 6.0, 'volume_reversal': 12.0, 'pullback_support': 14.0,
    },
    "finance": {
        # 歸因最強：VolumeReversal +15.4%勝率提升、PullbackSupport +14.9%
        # bollinger:5→2(-3), mfi:5→2(-3) 釋出6，補到 volume_reversal(+4)、pullback(+2)
        'rsi': 20.0, 'macd': 25.0, 'bollinger': 2.0,
        'mfi': 2.0, 'ema_cross': 25.0, 'volume': 10.0, 'adx': 10.0,
        'stoch_rsi': 10.0, 'volume_reversal': 12.0, 'pullback_support': 17.0,
    },
    "traditional": {
        # 歸因最強：Volume +26.6%勝率提升、EMA Cross +8.2%
        # 負貢獻：Pullback(-10.4%)、VolumeReversal(-9.9%) → 大幅降低
        # bollinger:5→2(-3), mfi:5→2(-3), pullback:8→3(-5), volume_reversal:18→10(-8) 釋出19
        # 補到 volume(+9)、ema_cross(+8)、macd(+2)
        'rsi': 10.0, 'macd': 17.0, 'bollinger': 2.0,
        'mfi': 2.0, 'ema_cross': 38.0, 'volume': 19.0, 'adx': 25.0,
        'stoch_rsi': 6.0, 'volume_reversal': 10.0, 'pullback_support': 3.0,
    },
    "precision": {
        # 精密機械/工業自動化（亞德客-KY、上銀、研華）
        # 歸因：MACD+EMA 穩健動能主導（亞德客-KY 回測最佳策略）
        'rsi': 22.0, 'macd': 28.0, 'bollinger': 2.0,
        'mfi': 2.0, 'ema_cross': 28.0, 'volume': 8.0, 'adx': 8.0,
        'stoch_rsi': 8.0, 'volume_reversal': 8.0, 'pullback_support': 15.0,
    },
    "default": {  # 其他（生技、ETF 等）：通用台股權重
        'rsi': 10.0, 'macd': 15.0, 'bollinger': 3.0,
        'mfi': 5.0, 'ema_cross': 18.0, 'volume': 25.0, 'adx': 14.0,
        'stoch_rsi': 8.0, 'volume_reversal': 15.0, 'pullback_support': 14.0,
    },
}

# ── 技術面四大柱面指標分組 ──
# 用於拆解技術分數為趨勢/動能/量能/支撐四個細項
TECH_PILLARS = {
    "trend":    ["ema_cross", "adx"],             # 趨勢面：EMA均線排列 + ADX趨勢強度
    "momentum": ["macd", "rsi", "stoch_rsi"],     # 動能面：MACD + RSI + 隨機RSI
    "volume":   ["volume", "volume_reversal"],    # 量能面：成交量 + 爆量反轉
    "support":  ["pullback_support", "bollinger", "mfi"],  # 支撐面：均線拉回 + 布林 + MFI
}

# 指標 display name (self.name) → 權重字典 key
INDICATOR_NAME_TO_KEY = {
    'EMA Cross':       'ema_cross',
    'ADX':             'adx',
    'MACD':            'macd',
    'RSI':             'rsi',
    'Stoch RSI':       'stoch_rsi',
    'Volume':          'volume',
    'Volume Reversal': 'volume_reversal',
    'Pullback Support':'pullback_support',
    'Bollinger Bands': 'bollinger',
    'MFI':             'mfi',
}


def compute_tech_pillar_scores(signal, sector_weights: dict) -> dict:
    """
    從 AggregatedSignal 中提取四大技術柱面分數 (0-100)

    計算邏輯：
    - BUY 信號：score / max_score * 100（越買越高）
    - SELL 信號：此指標得 0 分
    - NEUTRAL 信號：此指標得 50 分
    各柱面取所含指標的加權平均（以各指標的 sector_weights 為權重）
    """
    ind_scores: dict = {}

    for sig in signal.buy_signals:
        key = INDICATOR_NAME_TO_KEY.get(sig.indicator_name)
        if not key:
            continue
        max_s = sector_weights.get(key, 10.0)
        ind_scores[key] = min(100.0, sig.score / max_s * 100) if max_s > 0 else 100.0

    for sig in signal.sell_signals:
        key = INDICATOR_NAME_TO_KEY.get(sig.indicator_name)
        if key and key not in ind_scores:
            ind_scores[key] = 0.0  # 賣出信號 → 多頭貢獻為 0

    for sig in signal.neutral_signals:
        key = INDICATOR_NAME_TO_KEY.get(sig.indicator_name)
        if key and key not in ind_scores:
            ind_scores[key] = 50.0  # 中性 → 50

    pillar_scores: dict = {}
    for pillar, keys in TECH_PILLARS.items():
        weights_in_pillar = {k: sector_weights.get(k, 10.0) for k in keys}
        total_w = sum(weights_in_pillar.values())
        if total_w == 0:
            pillar_scores[pillar] = 50.0
            continue
        score = sum(ind_scores.get(k, 50.0) * weights_in_pillar[k] for k in keys) / total_w
        pillar_scores[pillar] = round(score, 1)

    return pillar_scores


# ── 各產業綜合分數五維權重 ──
# 依據：Regime 回測結果 + 產業特性推理
SECTOR_COMPOSITE_WEIGHTS = {
    "semiconductor": {  # 法人主導、趨勢明確、regime 回測夏普+0.89
        "chipflow": 0.35, "technical": 0.25, "fundamental": 0.15,
        "regime": 0.18, "sentiment": 0.07,
        "active_etf": 0.07,
    },
    "electronics": {  # 同半導體，regime 回測效果最強（夏普+0.94）
        "chipflow": 0.35, "technical": 0.25, "fundamental": 0.15,
        "regime": 0.18, "sentiment": 0.07,
        "active_etf": 0.07,
    },
    "finance": {  # 殖利率重要、波動小；籌碼回測有害(夏普-0.19)，降權
        "chipflow": 0.15, "technical": 0.20, "fundamental": 0.38,
        "regime": 0.13, "sentiment": 0.14,
        "active_etf": 0.03,  # 金融股少被主動ETF持有，權重低
    },
    "traditional": {  # regime 回測有害（夏普-0.36），基本面對景氣循環股重要
        "chipflow": 0.30, "technical": 0.25, "fundamental": 0.30,
        "regime": 0.05, "sentiment": 0.10,
        "active_etf": 0.03,
    },
    "precision": {  # 精密機械/工業自動化：動能主導，技術面權重高
        "chipflow": 0.28, "technical": 0.30, "fundamental": 0.18,
        "regime": 0.15, "sentiment": 0.07,
        "active_etf": 0.05,
    },
    "default": {  # 通用
        "chipflow": 0.35, "fundamental": 0.20, "technical": 0.25,
        "regime": 0.13, "sentiment": 0.07,
        "active_etf": 0.05,
    },
    # active_etf: 被持有才計入，未持有時該維度自動排除並重分配其他維度權重
}

# ── 股票代碼 → 產業分類 ──
SYMBOL_SECTOR_MAP = {}

# 半導體
for _s in ["2330.TW", "2454.TW", "2303.TW", "3711.TW", "2379.TW", "3034.TW",
           "6415.TW", "2344.TW", "3529.TW", "5274.TW", "2408.TW", "6770.TW",
           "3443.TW", "6515.TW"]:
    SYMBOL_SECTOR_MAP[_s] = "semiconductor"

# 電子代工 / AI / 零組件
for _s in ["2317.TW", "2382.TW", "2308.TW", "2357.TW", "3008.TW", "2345.TW",
           "3231.TW", "2356.TW", "4938.TW", "6669.TW",
           "3037.TW", "2327.TW", "3661.TW", "2376.TW", "3017.TW", "2353.TW",
           "6488.TW", "2301.TW", "2474.TW", "8046.TW", "3653.TW",
           "2383.TW", "2368.TW", "3665.TW"]:
    SYMBOL_SECTOR_MAP[_s] = "electronics"

# 精密機械 / 工業自動化
for _s in ["1590.TW", "2049.TW", "2395.TW"]:
    SYMBOL_SECTOR_MAP[_s] = "precision"

# 金融
for _s in ["2881.TW", "2882.TW", "2891.TW", "2886.TW", "2884.TW", "2880.TW",
           "2887.TW", "2890.TW", "2883.TW", "2892.TW", "5880.TW", "2885.TW"]:
    SYMBOL_SECTOR_MAP[_s] = "finance"

# 傳產 / 航運 / 鋼鐵 / 塑化 / 食品 / 電信
for _s in ["1301.TW", "2002.TW", "1216.TW", "2603.TW", "2609.TW", "2615.TW",
           "1303.TW", "1326.TW", "1101.TW", "2207.TW", "9910.TW",
           "2412.TW", "3045.TW", "4904.TW", "2912.TW",
           "1513.TW", "6505.TW", "2618.TW"]:
    SYMBOL_SECTOR_MAP[_s] = "traditional"


def get_sector_weights(symbol: str) -> dict:
    """取得股票對應的產業最佳技術面權重"""
    sector = SYMBOL_SECTOR_MAP.get(symbol, "default")
    return SECTOR_WEIGHTS[sector]


def get_symbol_sector(symbol: str) -> str:
    """取得股票所屬產業 ID"""
    return SYMBOL_SECTOR_MAP.get(symbol, "default")


# ── 自選股持久化 ──

CUSTOM_STOCKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "custom_stocks.json")


def load_custom_stocks() -> dict:
    """從 JSON 檔載入使用者自選股 {symbol: name}"""
    try:
        if os.path.exists(CUSTOM_STOCKS_FILE):
            with open(CUSTOM_STOCKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"載入自選股失敗: {e}")
    return {}


def save_custom_stocks(stocks: dict):
    """儲存自選股到 JSON"""
    try:
        with open(CUSTOM_STOCKS_FILE, "w", encoding="utf-8") as f:
            json.dump(stocks, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"儲存自選股失敗: {e}")


def add_custom_stock(symbol: str, name: str) -> bool:
    """新增自選股（若已在內建宇宙中則跳過）"""
    if symbol in _BUILTIN_UNIVERSE:
        return False  # 已是內建股
    stocks = load_custom_stocks()
    if symbol in stocks:
        return False  # 已加過
    stocks[symbol] = name
    save_custom_stocks(stocks)
    # 同步更新執行期宇宙
    SCREENER_UNIVERSE[symbol] = name
    logger.info(f"自選股已新增: {symbol} {name}")
    return True


def remove_custom_stock(symbol: str) -> bool:
    """移除自選股"""
    stocks = load_custom_stocks()
    if symbol not in stocks:
        return False
    del stocks[symbol]
    save_custom_stocks(stocks)
    SCREENER_UNIVERSE.pop(symbol, None)
    logger.info(f"自選股已移除: {symbol}")
    return True


def get_custom_stocks() -> dict:
    """取得所有自選股"""
    return load_custom_stocks()


# ── 選股宇宙（約 100 檔台股權值+熱門股） ──

_BUILTIN_UNIVERSE = {
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
    # 電信
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

# 合併內建 + 自選股 → 實際使用的宇宙
SCREENER_UNIVERSE = {**_BUILTIN_UNIVERSE, **load_custom_stocks()}

# ── 快取 ──

_screener_cache: Dict = {}  # {"results": [...], "categories": [...], "updated_at": str}
SCREENER_CACHE_TTL = 3600 * 6  # 6 小時
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "screener_cache.json")
RANK_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "screener_rank_history.json")


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

        # ── 1. 基本面（成長/價值雙軌） ──
        code = _strip_tw(symbol)
        pe_info = all_pe.get(code)
        fund_score = 50
        pe = None

        if pe_info and pe_info.get("pe") and pe_info["pe"] > 0:
            pe = pe_info["pe"]
            dy = pe_info.get("dy")
            details["pe"] = pe
            details["dy"] = dy

            from layers.fundamental import (fetch_twse_revenue_all, get_sector_pe_stats,
                                            compute_fundamental_score)
            all_rev = fetch_twse_revenue_all()
            rev_info = all_rev.get(code, {})
            sector = rev_info.get("sector")
            yoy = rev_info.get("yoy")
            mom = rev_info.get("mom")
            details["sector"] = sector
            details["yoy"] = yoy
            details["mom"] = mom

            # 產業百分位（若可取得）
            pe_percentile = None
            if sector:
                same_sector_symbols = [f"{c}.TW" for c, v in all_rev.items() if v.get("sector") == sector]
                if len(same_sector_symbols) >= 3:
                    pe_stats = get_sector_pe_stats(same_sector_symbols, all_pe)
                    sym_key = f"{code}.TW"
                    if sym_key in pe_stats:
                        pe_percentile = pe_stats[sym_key].get("percentile")
                        details["pe_percentile"] = pe_percentile

            # 統一評分函數
            fund_result = compute_fundamental_score(
                pe=pe, dy=dy, yoy=yoy, mom=mom, pe_percentile=pe_percentile)
            fund_score = fund_result["score"]
            details["peg"] = fund_result["peg"]
            details["fund_track"] = fund_result["track"]
            details["fund_advice"] = fund_result["advice"]

        scores["fundamental"] = fund_score

        # ── 2. 籌碼面（先取摘要，待取得收盤價後再算分數）──
        chip_summary = fetch_chip_summary(symbol, days=10)

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

            # 技術面四柱面細項分數
            tech_pillars = compute_tech_pillar_scores(signal, sector_w)
            details["tech_pillars"] = tech_pillars

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

        # ── 2b. 籌碼面評分（有價格資料後才計算）──
        close_price = float(df['close'].iloc[-1]) if df is not None and len(df) > 0 else None

        # 建立「日期(YYYYMMDD) → 收盤價」對照表，供逐日金額計算
        price_map: dict = {}
        if df is not None and len(df) > 0:
            for ts, row in df.iterrows():
                try:
                    date_key = ts.strftime("%Y%m%d") if hasattr(ts, 'strftime') else str(ts)[:10].replace("-", "")
                    price_map[date_key] = float(row['close'])
                except Exception:
                    pass

        chip_score = 50
        if chip_summary:
            chip_result = compute_chip_score(chip_summary, close_price=close_price)
            chip_score = chip_result["score"]
            foreign_net = chip_summary.get("foreign_total_net", 0)
            trust_net = chip_summary.get("trust_total_net", 0)

            # 逐日計算金額：每天淨買超股數 × 當天收盤價（更準確）
            daily_full = chip_summary.get("daily_data_full", [])
            foreign_net_amount = None
            trust_net_amount = None
            if daily_full and price_map:
                f_amt = sum(
                    d["foreign_net"] * price_map.get(d["date"], close_price or 0)
                    for d in daily_full
                    if price_map.get(d["date"], close_price)
                )
                t_amt = sum(
                    d["trust_net"] * price_map.get(d["date"], close_price or 0)
                    for d in daily_full
                    if price_map.get(d["date"], close_price)
                )
                foreign_net_amount = round(f_amt)
                trust_net_amount = round(t_amt)
            elif close_price:
                # fallback：無逐日資料時用最新收盤價近似
                foreign_net_amount = round(foreign_net * close_price)
                trust_net_amount = round(trust_net * close_price)

            details["chipflow"] = {
                "label": chip_result["label"],
                "foreign_consec_buy": chip_summary.get("foreign_consec_buy", 0),
                "trust_consec_buy": chip_summary.get("trust_consec_buy", 0),
                "foreign_total_net": foreign_net,
                "trust_total_net": trust_net,
                "foreign_net_amount": foreign_net_amount,
                "trust_net_amount": trust_net_amount,
                "margin_change_sum": chip_summary.get("margin_change_sum", 0),
                "close_price": close_price,
            }
        scores["chipflow"] = chip_score

        # ── 4. 主動式 ETF 評分（未被持有時設為 None，不列入綜合評分）──
        active_etf_score = get_active_etf_score(symbol)
        scores["active_etf"] = active_etf_score

        # ── 5. 消息面（無相關新聞時設為 None，不列入綜合評分）──
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

        # ── 6. 綜合分數（按產業使用不同維度權重）──
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
        fa = details.get("chipflow", {}).get("foreign_net_amount")
        ta = details.get("chipflow", {}).get("trust_net_amount")
        if fc >= 3:
            amt_str = f"，{_format_amount(fa)}" if fa else ""
            highlights.append(f"外資連買{fc}天{amt_str}")
        if tc >= 3:
            amt_str = f"，{_format_amount(ta)}" if ta else ""
            highlights.append(f"投信連買{tc}天{amt_str}")
        if details.get("pe_percentile") is not None and details["pe_percentile"] <= 40:
            highlights.append(f"產業低本益比(擊敗{100-details['pe_percentile']}%同業)")
        elif details.get("pe") and details["pe"] < 12:
            highlights.append(f"低本益比{details['pe']:.1f}")
        if regime_state in ("強勢多頭", "底部轉強"):
            highlights.append(f"盤勢{regime_state}")
        if active_etf_score is not None and active_etf_score >= 70:
            highlights.append("主動ETF重倉")

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

    # ── 百分位正規化：讓五個面向有相同的分佈基準 ──
    # 避免某些面向天然偏高/偏低而在加權時不公平
    dimensions = ["technical", "fundamental", "chipflow", "regime", "sentiment"]

    for dim in dimensions:
        # 收集有效分數
        valid_scores = []
        for r in results:
            s = r["scores"].get(dim)
            if s is not None:
                valid_scores.append(s)

        if len(valid_scores) < 5:
            continue  # 樣本太少，不做正規化

        valid_scores_sorted = sorted(valid_scores)
        n = len(valid_scores_sorted)

        for r in results:
            raw = r["scores"].get(dim)
            if raw is None:
                continue
            # 保留原始分數
            r.setdefault("raw_scores", {})[dim] = raw
            # 百分位排名（0-100）：在所有股票中贏過多少比例
            rank = sum(1 for v in valid_scores_sorted if v < raw)
            percentile = round(rank / n * 100, 1)
            r["scores"][dim] = percentile

    # 重新計算綜合分數（用正規化後的分數）
    for r in results:
        symbol_sector = get_symbol_sector(r["symbol"])
        weights = SECTOR_COMPOSITE_WEIGHTS.get(symbol_sector, SECTOR_COMPOSITE_WEIGHTS["default"])
        valid = [(r["scores"].get(k, 50), w) for k, w in weights.items()
                 if r["scores"].get(k) is not None]
        if not valid:
            valid = [(50, 1.0)]
        total_w = sum(w for _, w in valid)
        r["composite"] = round(sum(s * w for s, w in valid) / total_w, 1)

    # 依綜合分數排序
    results.sort(key=lambda x: x["composite"], reverse=True)

    elapsed = time.time() - start_time
    logger.info(f"選股掃描完成: {len(results)} 檔，耗時 {elapsed:.1f}秒（含百分位正規化）")

    return results


def categorize_picks(results: List[dict]) -> List[dict]:
    """
    從掃描結果中篩選五大精選類別

    每類最多 5 檔，依綜合分數排序
    """
    categories = []
    rank_history = _load_rank_history()
    today = datetime.now().strftime("%Y-%m-%d")

    def _annotate_days(picks_list: List[dict], cat_id: str) -> List[dict]:
        """為每支股票標記入榜天數"""
        symbols = [p["symbol"] for p in picks_list]
        days_map = _update_rank_history_for_category(rank_history, cat_id, symbols, today)
        for p in picks_list:
            p["_days_in_rank"] = days_map.get(p["symbol"], 1)
        return picks_list

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
    _annotate_days(top_ranked, "top_ranked")
    categories.append({
        "id": "top_ranked",
        "name": "綜合排行榜",
        "icon": "👑",
        "description": "綜合分數 = 籌碼×權重 + 技術×權重 + 基本面×權重 + 盤勢×權重 + 消息×權重。各產業權重不同，例：半導體/電子 籌碼35%，金融 基本面38% 為主。排名依綜合分數由高到低。",
        "score_field": "composite",
        "score_label": "綜合",
        "stocks": _format_picks(top_ranked),
    })

    # ── 1a. 外資狂買股（依籌碼分數排序，連買 >= 3 天）──
    foreign_chip_picks = []
    for r in results:
        fc = r.get("details", {}).get("chipflow", {}).get("foreign_consec_buy", 0)
        fa = r.get("details", {}).get("chipflow", {}).get("foreign_net_amount")
        if fc >= 3:
            amt_str = f"，累計{_format_amount(fa)}" if fa else ""
            r["_highlight"] = f"外資連買{fc}天{amt_str}"
            foreign_chip_picks.append(r)
    foreign_chip_picks.sort(key=lambda x: x.get("scores", {}).get("chipflow", 0), reverse=True)
    _annotate_days(foreign_chip_picks[:10], "foreign_buy")
    categories.append({
        "id": "foreign_buy",
        "name": "外資狂買股",
        "icon": "🔥",
        "description": "篩選條件：外資連續買超 ≥ 3 天，依籌碼面綜合分數排序。籌碼分數綜合外資連買天數、投信方向、融資券變化等多維訊號，分數高代表整體籌碼最強。",
        "score_field": "scores.chipflow",
        "score_label": "籌碼",
        "stocks": _format_picks(foreign_chip_picks[:10]),
    })

    # ── 1b. 外資連買股（依連買天數排序）──
    foreign_days_picks = []
    for r in results:
        fc = r.get("details", {}).get("chipflow", {}).get("foreign_consec_buy", 0)
        fa = r.get("details", {}).get("chipflow", {}).get("foreign_net_amount")
        if fc >= 3:
            amt_str = f"，累計{_format_amount(fa)}" if fa else ""
            r["_highlight"] = f"外資連買{fc}天{amt_str}"
            foreign_days_picks.append(r)
    foreign_days_picks.sort(key=lambda x: x.get("details", {}).get("chipflow", {}).get("foreign_consec_buy", 0), reverse=True)
    _annotate_days(foreign_days_picks[:10], "foreign_days")
    categories.append({
        "id": "foreign_days",
        "name": "外資連買股",
        "icon": "🏦",
        "description": "依外資連續買超天數排序。連買天數越多代表外資持續看好、分批佈局，是中長線信心指標。",
        "score_field": "chipflow.foreign_consec_buy",
        "score_label": "連買天數",
        "stocks": _format_picks(foreign_days_picks[:10]),
    })

    # ── 1d. 外資買超金額（依累計金額排序）──
    foreign_amount_picks = []
    for r in results:
        fa = r.get("details", {}).get("chipflow", {}).get("foreign_net_amount")
        fc = r.get("details", {}).get("chipflow", {}).get("foreign_consec_buy", 0)
        if fa and fa > 0:
            days_str = f"，連買{fc}天" if fc >= 2 else ""
            r["_highlight"] = f"外資累計買超{_format_amount(fa)}{days_str}"
            foreign_amount_picks.append(r)
    foreign_amount_picks.sort(key=lambda x: x.get("details", {}).get("chipflow", {}).get("foreign_net_amount", 0), reverse=True)
    _annotate_days(foreign_amount_picks[:10], "foreign_amount")
    categories.append({
        "id": "foreign_amount",
        "name": "外資買超金額",
        "icon": "💰",
        "description": "依外資近 10 日累計淨買超金額排序（股數 × 收盤價）。金額越大代表外資投入的真實資金越多，反映實質買盤力道。",
        "score_field": "chipflow.foreign_net_amount",
        "score_label": "買超金額",
        "stocks": _format_picks(foreign_amount_picks[:10]),
    })

    # ── 2a. 投信狂買股（依籌碼分數排序，連買 >= 3 天）──
    trust_chip_picks = []
    for r in results:
        tc = r.get("details", {}).get("chipflow", {}).get("trust_consec_buy", 0)
        ta = r.get("details", {}).get("chipflow", {}).get("trust_net_amount")
        if tc >= 3:
            amt_str = f"，累計{_format_amount(ta)}" if ta else ""
            r["_highlight"] = f"投信連買{tc}天{amt_str}"
            trust_chip_picks.append(r)
    trust_chip_picks.sort(key=lambda x: x.get("scores", {}).get("chipflow", 0), reverse=True)
    _annotate_days(trust_chip_picks[:10], "trust_buy")
    categories.append({
        "id": "trust_buy",
        "name": "投信狂買股",
        "icon": "🔥",
        "description": "篩選條件：投信連續買超 ≥ 3 天，依籌碼面綜合分數排序。籌碼分數綜合投信連買天數、外資方向、融資券變化等多維訊號，分數高代表整體籌碼最強。",
        "score_field": "scores.chipflow",
        "score_label": "籌碼",
        "stocks": _format_picks(trust_chip_picks[:10]),
    })

    # ── 2b. 投信連買股（依連買天數排序）──
    trust_days_picks = []
    for r in results:
        tc = r.get("details", {}).get("chipflow", {}).get("trust_consec_buy", 0)
        ta = r.get("details", {}).get("chipflow", {}).get("trust_net_amount")
        if tc >= 3:
            amt_str = f"，累計{_format_amount(ta)}" if ta else ""
            r["_highlight"] = f"投信連買{tc}天{amt_str}"
            trust_days_picks.append(r)
    trust_days_picks.sort(key=lambda x: x.get("details", {}).get("chipflow", {}).get("trust_consec_buy", 0), reverse=True)
    _annotate_days(trust_days_picks[:10], "trust_days")
    categories.append({
        "id": "trust_days",
        "name": "投信連買股",
        "icon": "🎯",
        "description": "依投信連續買超天數排序。投信選股嚴謹，連續買超代表有基本面研究支撐，是中期波段的領先指標。",
        "score_field": "chipflow.trust_consec_buy",
        "score_label": "連買天數",
        "stocks": _format_picks(trust_days_picks[:10]),
    })

    # ── 2c. 投信買超金額（依累計金額排序）──
    trust_amount_picks = []
    for r in results:
        ta = r.get("details", {}).get("chipflow", {}).get("trust_net_amount")
        tc = r.get("details", {}).get("chipflow", {}).get("trust_consec_buy", 0)
        if ta and ta > 0:
            days_str = f"，連買{tc}天" if tc >= 2 else ""
            r["_highlight"] = f"投信累計買超{_format_amount(ta)}{days_str}"
            trust_amount_picks.append(r)
    trust_amount_picks.sort(key=lambda x: x.get("details", {}).get("chipflow", {}).get("trust_net_amount", 0), reverse=True)
    _annotate_days(trust_amount_picks[:10], "trust_amount")
    categories.append({
        "id": "trust_amount",
        "name": "投信買超金額",
        "icon": "🎯💰",
        "description": "依投信近 10 日累計淨買超金額排序（股數 × 收盤價）。投信資金規模雖不如外資，但選股精準度高，金額大代表高度認同。",
        "score_field": "chipflow.trust_net_amount",
        "score_label": "買超金額",
        "stocks": _format_picks(trust_amount_picks[:10]),
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
    chip_concentrated.sort(key=lambda x: x.get("scores", {}).get("chipflow", 0), reverse=True)
    _annotate_days(chip_concentrated[:10], "chip_concentrated")
    categories.append({
        "id": "chip_concentrated",
        "name": "籌碼集中股",
        "icon": "🔒",
        "description": "篩選條件：融資餘額減少 > 500 張，且外資或投信同步買超。融資減少代表散戶離場、籌碼沉澱到法人手中，是股價醞釀上漲的前兆。",
        "score_field": "scores.chipflow",
        "score_label": "籌碼",
        "stocks": _format_picks(chip_concentrated[:10]),
    })

    # ── 4. 價值低估股 ──
    value_picks = []
    for r in results:
        pe = r.get("details", {}).get("pe")
        pe_percentile = r.get("details", {}).get("pe_percentile")
        fund_score = r.get("scores", {}).get("fundamental", 0)
        
        # 價值股軌道：用原始分數判斷（非正規化後）
        raw_fund = r.get("raw_scores", {}).get("fundamental", fund_score)
        fund_track = r.get("details", {}).get("fund_track", "value")
        if fund_track == "value" and raw_fund >= 70 and (
                (pe and pe < 12) or (pe_percentile is not None and pe_percentile <= 30)):
            if pe_percentile is not None and pe_percentile <= 30:
                r["_highlight"] = f"產業低估前{pe_percentile}%，基本面{fund_score}分"
            else:
                r["_highlight"] = f"P/E {pe:.1f}，基本面{fund_score}分"
            value_picks.append(r)
    value_picks.sort(key=lambda x: x.get("scores", {}).get("fundamental", 0), reverse=True)
    _annotate_days(value_picks[:10], "value_underpriced")
    categories.append({
        "id": "value_underpriced",
        "name": "價值低估股",
        "icon": "💎",
        "description": "篩選條件：價值股軌道 + 本益比 < 12 或產業估值前 30% 低估，且基本面分數 ≥ 70。適合穩健型投資人。",
        "score_field": "scores.fundamental",
        "score_label": "基本面",
        "stocks": _format_picks(value_picks[:10]),
    })

    # ── 5. 成長動能股 ──
    growth_picks = []
    for r in results:
        fund_track = r.get("details", {}).get("fund_track", "value")
        raw_fund = r.get("raw_scores", {}).get("fundamental", r.get("scores", {}).get("fundamental", 0))
        peg = r.get("details", {}).get("peg")
        yoy = r.get("details", {}).get("yoy")

        if fund_track == "growth" and raw_fund >= 65 and peg is not None:
            parts = [f"PEG={peg}"]
            if yoy is not None:
                parts.append(f"營收YoY+{yoy:.0f}%")
            parts.append(f"基本面{fund_score}分")
            r["_highlight"] = "，".join(parts)
            growth_picks.append(r)
    growth_picks.sort(key=lambda x: x.get("details", {}).get("peg", 99))
    _annotate_days(growth_picks[:10], "growth_momentum")
    categories.append({
        "id": "growth_momentum",
        "name": "成長動能股",
        "icon": "📈",
        "description": "篩選條件：營收 YoY > 15% 且 PEG < 1.5（P/E ÷ 營收成長率）。高成長但估值合理的股票，適合積極型投資人。PEG < 1 表示成長遠超估值。",
        "score_field": "scores.fundamental",
        "score_label": "基本面",
        "stocks": _format_picks(growth_picks[:10]),
    })

    # ── 6. 技術突破股 ──
    tech_picks = []
    for r in results:
        regime = r.get("details", {}).get("regime_state", "")
        raw_tech = r.get("raw_scores", {}).get("technical", r.get("scores", {}).get("technical", 0))
        if regime in ("強勢多頭", "底部轉強") and raw_tech >= 55:
            r["_highlight"] = f"盤勢{regime}＋技術{raw_tech}分"
            tech_picks.append(r)
    tech_picks.sort(key=lambda x: x.get("scores", {}).get("technical", 0), reverse=True)
    _annotate_days(tech_picks[:10], "tech_breakout")
    categories.append({
        "id": "tech_breakout",
        "name": "技術突破股",
        "icon": "🚀",
        "description": "篩選條件：盤勢狀態為「強勢多頭」或「底部轉強」，且原始技術分數 ≥ 55（非百分位）。技術分數綜合 10 個指標：EMA 趨勢、ADX 動能、MACD、RSI、Stoch RSI（拉回超賣）、Volume Reversal（爆量反轉）、Pullback Support（均線拉回）等，各產業權重依回測歸因分析調整。",
        "score_field": "scores.technical",
        "score_label": "技術",
        "stocks": _format_picks(tech_picks[:10]),
    })

    # ── 7. 趨勢強攻股（技術細項：EMA+ADX 趨勢柱面）──
    trend_picks = []
    for r in results:
        trend_score = r.get("details", {}).get("tech_pillars", {}).get("trend", 0)
        regime = r.get("details", {}).get("regime_state", "")
        if trend_score >= 60 and regime not in ("空頭", "高檔轉折"):
            r["_highlight"] = f"趨勢面{trend_score:.0f}分（EMA+ADX）｜{regime}"
            trend_picks.append(r)
    trend_picks.sort(key=lambda x: x.get("details", {}).get("tech_pillars", {}).get("trend", 0), reverse=True)
    _annotate_days(trend_picks[:10], "trend_follow")
    categories.append({
        "id": "trend_follow",
        "name": "趨勢強攻股",
        "icon": "📐",
        "description": "技術細項排行：EMA 均線多頭排列 + ADX 趨勢強度雙雙高分（趨勢柱面 ≥ 60）。均線多排代表中長線方向確立，ADX 確認趨勢而非震盪，是追多趨勢的最佳信號組合。",
        "score_field": "tech_pillars.trend",
        "score_label": "趨勢",
        "stocks": _format_picks(trend_picks[:10]),
    })

    # ── 8. 量能異動股（技術細項：Volume+VolumeReversal 量能柱面）──
    volume_picks = []
    for r in results:
        vol_score = r.get("details", {}).get("tech_pillars", {}).get("volume", 0)
        regime = r.get("details", {}).get("regime_state", "")
        if vol_score >= 65 and regime not in ("空頭",):
            r["_highlight"] = f"量能面{vol_score:.0f}分（成交量+爆量反轉）｜{regime}"
            volume_picks.append(r)
    volume_picks.sort(key=lambda x: x.get("details", {}).get("tech_pillars", {}).get("volume", 0), reverse=True)
    _annotate_days(volume_picks[:10], "volume_surge")
    categories.append({
        "id": "volume_surge",
        "name": "量能異動股",
        "icon": "💥",
        "description": "技術細項排行：成交量放大 + 爆量反轉信號（量能柱面 ≥ 65）。量是價的先行指標，法人悄悄進場時必伴隨異常大量，爆量反轉更是主力換手的明確訊號。",
        "score_field": "tech_pillars.volume",
        "score_label": "量能",
        "stocks": _format_picks(volume_picks[:10]),
    })

    # ── 9. 盤勢強攻股（盤勢細項：強勢多頭 + 多頭）──
    regime_bull_picks = []
    for r in results:
        regime = r.get("details", {}).get("regime_state", "")
        if regime in ("強勢多頭", "多頭"):
            regime_score = r.get("scores", {}).get("regime", 0)
            raw_tech = r.get("raw_scores", {}).get("technical", r.get("scores", {}).get("technical", 0))
            r["_highlight"] = f"盤勢{regime}（{regime_score:.0f}分）｜技術{raw_tech:.0f}分"
            regime_bull_picks.append(r)
    regime_bull_picks.sort(key=lambda x: x.get("scores", {}).get("regime", 0), reverse=True)
    _annotate_days(regime_bull_picks[:10], "regime_bull")
    categories.append({
        "id": "regime_bull",
        "name": "盤勢強攻股",
        "icon": "🔥",
        "description": "盤勢細項排行：盤勢偵測為「強勢多頭」或「多頭」。判斷依據：5/10/20/60日均線完美多頭排列、波段高低點持續墊高、ADX趨勢強度確認。均線越整齊＋趨勢越強，多方越安全。",
        "score_field": "scores.regime",
        "score_label": "盤勢",
        "stocks": _format_picks(regime_bull_picks[:10]),
    })

    # ── 10. 底部轉強股（盤勢細項：底部反轉）──
    reversal_picks = []
    for r in results:
        regime = r.get("details", {}).get("regime_state", "")
        if regime == "底部轉強":
            raw_tech = r.get("raw_scores", {}).get("technical", r.get("scores", {}).get("technical", 0))
            r["_highlight"] = f"盤勢底部轉強｜技術{raw_tech:.0f}分"
            reversal_picks.append(r)
    reversal_picks.sort(key=lambda x: x.get("scores", {}).get("regime", 0), reverse=True)
    _annotate_days(reversal_picks[:10], "bottom_reversal")
    categories.append({
        "id": "bottom_reversal",
        "name": "底部轉強股",
        "icon": "🌱",
        "description": "盤勢細項排行：盤勢偵測確認為「底部轉強」。綜合判斷依據：均線底部排列（5日剛翻越10日）、成交量底部放大、K棒止跌訊號（長下影線/吞噬）等，是空轉多的黃金進場視窗。",
        "score_field": "scores.regime",
        "score_label": "盤勢",
        "stocks": _format_picks(reversal_picks[:10]),
    })

    # ── 11. 高檔轉折股（盤勢細項：風險示警）──
    regime_top_picks = []
    for r in results:
        regime = r.get("details", {}).get("regime_state", "")
        if regime in ("高檔轉折", "空頭"):
            raw_tech = r.get("raw_scores", {}).get("technical", r.get("scores", {}).get("technical", 0))
            r["_highlight"] = f"盤勢{regime}｜技術{raw_tech:.0f}分｜注意風險"
            regime_top_picks.append(r)
    regime_top_picks.sort(key=lambda x: x.get("scores", {}).get("regime", 100))  # regime 低分排前
    _annotate_days(regime_top_picks[:10], "regime_warning")
    categories.append({
        "id": "regime_warning",
        "name": "高檔示警股",
        "icon": "⚠️",
        "description": "盤勢細項排行：盤勢偵測為「高檔轉折」或「空頭」。判斷依據：均線死亡交叉、高位長黑K、波段高點不再創高。若持有這些標的建議留意停利或降低部位。",
        "score_field": "scores.regime",
        "score_label": "盤勢",
        "stocks": _format_picks(regime_top_picks[:10]),
    })

    _save_rank_history(rank_history)
    return categories


def _format_shares(shares: int) -> str:
    """格式化張數顯示"""
    if abs(shares) >= 10000:
        return f"{shares/10000:.1f}萬張"
    elif abs(shares) >= 1000:
        return f"{shares/1000:.1f}千張"
    else:
        return f"{shares}張"


def _format_amount(amount) -> str:
    """格式化金額顯示（元）"""
    if amount is None:
        return ""
    a = abs(amount)
    sign = "" if amount >= 0 else "-"
    if a >= 1_0000_0000:  # 億
        return f"{sign}{a / 1_0000_0000:.1f}億"
    elif a >= 1_0000:     # 萬
        return f"{sign}{a / 1_0000:.0f}萬"
    else:
        return f"{sign}{a:.0f}元"


def _format_picks(picks: List[dict]) -> List[dict]:
    """格式化精選股票為前端需要的格式"""
    formatted = []
    for p in picks:
        chip = p.get("details", {}).get("chipflow", {})
        formatted.append({
            "symbol": p["symbol"],
            "name": p["name"],
            "composite_score": p["composite"],
            "highlight": p.get("_highlight", ""),
            "scores": p["scores"],
            "raw_scores": p.get("raw_scores", {}),
            "days_in_rank": p.get("_days_in_rank", 1),
            "tech_pillars": p.get("details", {}).get("tech_pillars", {}),
            "regime_state": p.get("details", {}).get("regime_state", ""),
            "foreign_consec_buy": chip.get("foreign_consec_buy", 0),
            "trust_consec_buy": chip.get("trust_consec_buy", 0),
            "foreign_net_amount": chip.get("foreign_net_amount"),
            "trust_net_amount": chip.get("trust_net_amount"),
        })
    return formatted


def _load_rank_history() -> dict:
    """載入入榜歷史記錄 {category_id: {symbol: first_seen_date}}"""
    try:
        with open(RANK_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_rank_history(history: dict):
    """儲存入榜歷史記錄"""
    try:
        os.makedirs(os.path.dirname(RANK_HISTORY_FILE), exist_ok=True)
        with open(RANK_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"入榜歷史記錄存檔失敗: {e}")


_twii_trading_days_cache = []

def _get_trading_days_count(start_date: str, end_date: str) -> int:
    """計算台股實際開市工作天數（過濾假日與未開市日）"""
    global _twii_trading_days_cache
    try:
        from datetime import datetime, timedelta
        import pandas as pd
        
        if not _twii_trading_days_cache:
            import yfinance as yf
            end_dt = datetime.now()
            # 抓取過去一年的加權指數來判斷交易日
            start_dt = end_dt - timedelta(days=365)
            twii = yf.Ticker("^TWII")
            hist = twii.history(start=start_dt.strftime("%Y-%m-%d"), end=(end_dt + timedelta(days=1)).strftime("%Y-%m-%d"))
            if not hist.empty:
                _twii_trading_days_cache = [d.strftime("%Y-%m-%d") for d in hist.index]

        if _twii_trading_days_cache:
            count = sum(1 for d in _twii_trading_days_cache if start_date <= d <= end_date)
            # 若今日還沒收盤，yfinance 可能沒今天的 index，且若今天是平日，則自動補上 1 天
            if end_date not in _twii_trading_days_cache and start_date <= end_date and pd.to_datetime(end_date).weekday() < 5:
                 count += 1
            return max(1, count)
    except Exception:
        pass

    # Fallback: 單純使用 Pandas 計算扣除六日的工作天
    try:
        import pandas as pd
        return max(1, len(pd.bdate_range(start=start_date, end=end_date)))
    except Exception:
        return 1


def _update_rank_history_for_category(history: dict, cat_id: str, symbols: List[str],
                                       today: str) -> dict:
    """
    更新某類別的入榜歷史，並計算每支股票的入榜天數
    Returns: {symbol: days_in_rank}
    today: 預先計算好的日期字串 (YYYY-MM-DD)，避免 7 次呼叫重複計算
    """
    if cat_id not in history:
        history[cat_id] = {}

    cat_hist = history[cat_id]
    current_set = set(symbols)

    for sym in list(cat_hist.keys()):
        if sym not in current_set:
            del cat_hist[sym]

    for sym in symbols:
        if sym not in cat_hist:
            cat_hist[sym] = today

    days_map = {}
    for sym in symbols:
        first_seen = cat_hist.get(sym, today)
        try:
            delta = _get_trading_days_count(first_seen, today)
        except Exception:
            delta = 1
        days_map[sym] = max(1, delta)

    return days_map


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
            "raw_scores": r.get("raw_scores", {}),
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
