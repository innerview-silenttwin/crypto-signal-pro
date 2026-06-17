"""回測用：載入 FinMind 法人資料快取 + 計算每日 chip_score (0-100)。

資料來源：backend/data/backtest/finmind_inst_cache.json (51 symbols × ~1760 days × {foreign_net, trust_net, dealer_net})

⚠️ 簡化版 chip_score（vs production compute_chip_score）：
- production 用 5 個子分數：外資 30% + 投信 25% + 自營 10% + 融資 20% + 融券 15%
- 回測**只用前 3 個（法人類）= 65%，融資/融券忽略**（margin history 只有 2026-06 起，回測期 2021-2026 大部分缺）
- 三項分數重新 normalize 到 0-100

設計符合「production decision time 用前一日法人資料」的約束：
get_chip_score(symbol, decision_date) 用 decision_date - 1 的 cached 資料。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
CACHE_PATH = ROOT / "backend" / "data" / "backtest" / "finmind_inst_cache.json"


class ChipDataLoader:
    """全部 symbols 法人資料一次載入，cache by symbol → date → {foreign_net, trust_net, dealer_net}"""

    def __init__(self):
        with open(CACHE_PATH) as f:
            raw = json.load(f)
        # 正規化 key 格式：cache key 是 "2330"，date key 是 "20190102"
        # 轉成 {symbol: {date_obj: data}}
        self._data: Dict[str, Dict[date, dict]] = {}
        for code, by_date in raw.items():
            entries = {}
            for d_str, vals in by_date.items():
                try:
                    d_obj = datetime.strptime(d_str, "%Y%m%d").date()
                except ValueError:
                    continue
                entries[d_obj] = {
                    "foreign_net": int(vals.get("foreign_net", 0) or 0),
                    "trust_net": int(vals.get("trust_net", 0) or 0),
                    "dealer_net": int(vals.get("dealer_net", 0) or 0),
                }
            self._data[code] = entries
        print(f"[chip_data] 載入 {len(self._data)} symbols 法人快取")

    def has_symbol(self, symbol: str) -> bool:
        """symbol 帶 .TW 或純代號都可。"""
        return self._normalize(symbol) in self._data

    @staticmethod
    def _normalize(symbol: str) -> str:
        return symbol.split(".")[0]

    def get_summary(self, symbol: str, decision_date: date) -> Optional[dict]:
        """模擬 production fetch_chip_summary 的 output schema（簡化版）。

        關鍵約束：decision_date 當天的決策用 **前一交易日** 的法人資料。
        實作：往前找直到拿到資料的最後 N 天。

        Returns: dict 含 production compute_chip_score 需要的欄位（融資/融券缺，設 0）
                 None 代表無法產出有效 summary（資料太少）
        """
        code = self._normalize(symbol)
        entries = self._data.get(code)
        if not entries:
            return None

        # 找 decision_date 之前最多 35 個交易日的資料（30d momentum 需要）
        dates_sorted = sorted(d for d in entries if d < decision_date)
        if not dates_sorted:
            return None
        recent = dates_sorted[-35:]  # 30d momentum + 5d cumulative + 隔天

        # 連續買超天數（從最近往前數）
        def consec_buy(field: str) -> int:
            """正值代表連續買超天數；負值代表連續賣超天數。"""
            count = 0
            sign = None
            for d in reversed(recent):
                net = entries[d].get(field, 0)
                if net > 0:
                    if sign is None: sign = 1
                    if sign != 1: break
                    count += 1
                elif net < 0:
                    if sign is None: sign = -1
                    if sign != -1: break
                    count += 1
                else:
                    break
            return count if sign == 1 else -count

        # 累計 N 日淨買賣（最近 N 日，N 預設 5 對應 production"近期")
        def cumsum(field: str, n: int) -> int:
            return sum(entries[d].get(field, 0) for d in recent[-n:])

        return {
            "foreign_consec_buy": consec_buy("foreign_net"),
            "trust_consec_buy": consec_buy("trust_net"),
            "foreign_total_net": cumsum("foreign_net", 5),
            "trust_total_net": cumsum("trust_net", 5),
            "dealer_total_net": cumsum("dealer_net", 5),
            "foreign_30d_net": cumsum("foreign_net", 30),
            "trust_30d_net": cumsum("trust_net", 30),
            # 融資 / 融券無資料（cache 不含），交給 score 函式給中性處理
            "margin_change_sum": 0,
            "short_change_sum": 0,
        }

    def get_chip_score(self, symbol: str, decision_date: date, close_price: float = None) -> Optional[float]:
        """計算簡化版 chip_score (0-100)。

        Returns:
            float 0-100 或 None（無資料）
        """
        summary = self.get_summary(symbol, decision_date)
        if summary is None:
            return None
        return _compute_chip_score_simplified(summary, close_price)


def _compute_chip_score_simplified(summary: dict, close_price: float = None) -> float:
    """簡化版 chip_score（只用法人 3 維、忽略融資/融券）。

    對應 backend/layers/chipflow.py::compute_chip_score 的法人部分邏輯，
    但 normalize 到 0-100 區間（原 100 內含融資 20% + 融券 15%，這裡略掉）。
    """
    # ── 1. 外資 (原 30% → renorm 後 ~46%) ──
    fc = summary.get("foreign_consec_buy", 0)
    if fc >= 4:    foreign_score = 90
    elif fc >= 2:  foreign_score = 75
    elif fc >= 1:  foreign_score = 60
    elif fc == 0:  foreign_score = 50
    elif fc >= -2: foreign_score = 35
    elif fc >= -4: foreign_score = 25
    else:          foreign_score = 15

    ft = summary.get("foreign_total_net", 0)
    if close_price and close_price > 0:
        ft_amount = ft * close_price
        if ft_amount > 50_000_000:    foreign_score = min(100, foreign_score + 10)
        elif ft_amount < -50_000_000: foreign_score = max(0, foreign_score - 10)
    f30 = summary.get("foreign_30d_net", 0)
    if close_price and close_price > 0:
        f30_amount = f30 * close_price
        if f30_amount > 200_000_000:    foreign_score = min(100, foreign_score + 8)
        elif f30_amount < -200_000_000: foreign_score = max(0, foreign_score - 8)

    # ── 2. 投信 (原 25% → renorm 後 ~38%) ──
    tc = summary.get("trust_consec_buy", 0)
    if tc >= 4:    trust_score = 92
    elif tc >= 2:  trust_score = 85
    elif tc >= 1:  trust_score = 65
    elif tc == 0:  trust_score = 50
    elif tc >= -2: trust_score = 30
    else:          trust_score = 20

    # ── 3. 自營 (原 10% → renorm 後 ~16%) ──
    dt = summary.get("dealer_total_net", 0)
    if dt > 10000:    dealer_score = 70
    elif dt > 0:      dealer_score = 60
    elif dt == 0:     dealer_score = 50
    elif dt > -10000: dealer_score = 40
    else:             dealer_score = 30

    # 重新加權（原權重 30/25/10 = 65 → renorm 到 46/38/16）
    return foreign_score * 0.46 + trust_score * 0.38 + dealer_score * 0.16


# Module-level singleton（lazy init）
_loader: Optional[ChipDataLoader] = None


def get_loader() -> ChipDataLoader:
    global _loader
    if _loader is None:
        _loader = ChipDataLoader()
    return _loader
