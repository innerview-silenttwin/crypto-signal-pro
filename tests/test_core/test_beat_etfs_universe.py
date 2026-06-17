"""BEAT_ETFS + SECTOR_STOCKS["其他"] ETF 交易池 整合守門測試。

背景：2026-06-17 用戶確認把優於大盤的主動 ETF 加入交易池 + BEAT_ETFS 評分清單。

設計分層（**重要**）：
- **BEAT_ETFS**（評分用）：只收持台股的主動 ETF。原 8 檔 + 00982A = 9 檔。
  美股 ETF（00988A / 00990A 持 NVDA / AAPL）對台股加分為 0，**不放這裡**。
- **SECTOR_STOCKS["其他"]**（交易用）：6 檔保守版（含美股 ETF）— 系統可買賣這些 ETF 本身。

兩者語意不同：BEAT_ETFS 是「評分權威」、SECTOR_STOCKS 是「交易標的」。
美股 ETF 進交易池 OK（買 ETF 本身就是買它持的美股 indirectly），
但進 BEAT_ETFS 不對（它持的 NVDA 不能為台股 2330 加分）。

詳見：
- memory/project_active_etf_us.md
- memory/project_active_etf_00403a_pending.md
- backend/layers/active_etf.py BEAT_ETFS
"""

import pytest

from layers.active_etf import BEAT_ETFS, _TW_SID_RE
from sector_trader import SECTOR_STOCKS


# ── BEAT_ETFS 結構 ─────────────────────────────────────────

def test_beat_etfs_count_is_9():
    assert len(BEAT_ETFS) == 9, f"應該有 9 檔（原 8 + 00982A），實際 {len(BEAT_ETFS)}"


def test_beat_etfs_new_addition_present():
    """2026-06-17 加 00982A 一檔持台股的新進。"""
    codes = {e["code"] for e in BEAT_ETFS}
    assert "00982A" in codes


def test_beat_etfs_us_stock_etfs_excluded():
    """美股為主的主動 ETF（00988A / 00990A）不該在 BEAT_ETFS — 對台股加分為 0。

    這些 ETF 仍可在 SECTOR_STOCKS["其他"] 當交易標的，但不參與評分。
    """
    codes = {e["code"] for e in BEAT_ETFS}
    for us_etf in ["00988A", "00990A", "00983A", "00986A", "00989A"]:
        assert us_etf not in codes, f"美股 ETF {us_etf} 不該在 BEAT_ETFS"


def test_beat_etfs_existing_preserved():
    """既有 8 檔不應被刪掉。"""
    codes = {e["code"] for e in BEAT_ETFS}
    existing = ["00981A", "00994A", "00995A", "00992A", "00991A",
                "00985A", "00987A", "00980A"]
    for c in existing:
        assert c in codes, f"既有 {c} 不該被移除"


def test_beat_etfs_sorted_by_alpha_desc():
    """rank_weight 邏輯依 list 順序、所以必須 alpha 由高到低。"""
    alphas = [e["alpha_value"] for e in BEAT_ETFS]
    assert alphas == sorted(alphas, reverse=True), \
        f"BEAT_ETFS 應依 alpha_value 降序，目前={alphas}"


def test_beat_etfs_each_has_required_fields():
    """新 schema：code / name / alpha_value / alpha_window；舊 alpha 為向後相容。"""
    for e in BEAT_ETFS:
        assert "code" in e
        assert "name" in e
        assert "alpha_value" in e
        assert "alpha_window" in e
        assert e["alpha_window"] in ("6mo", "ytd"), f"非法 window: {e['alpha_window']}"
        assert isinstance(e["alpha_value"], (int, float))
        # 向後相容：舊 .alpha 仍存在
        assert "alpha" in e and e["alpha"] == e["alpha_value"]
        assert e["code"].endswith("A"), f"主動 ETF 代號應以 A 結尾：{e['code']}"


def test_beat_etfs_rank_weight_sums_to_one():
    """rank_weight 加總應為 1.0（用於 active_etf_score 加權平均）。"""
    total = sum(e.get("rank_weight", 0) for e in BEAT_ETFS)
    assert abs(total - 1.0) < 1e-6, f"rank_weight 加總應為 1.0，實際 {total}"


def test_00403a_still_not_in_beat_etfs():
    """00403A 從 2026-06-01 起一直延期、現況 -49pp、不該加入。

    若未來真的要加進去（2026-09-01 後評估），請更新此 test + memory。
    """
    codes = {e["code"] for e in BEAT_ETFS}
    assert "00403A" not in codes, "00403A 仍 pending，不該在 BEAT_ETFS"


# ── sid 過濾防呆（H1 第二道防線）─────────────────────────────────

@pytest.mark.parametrize("sid,valid", [
    ("2330", True),       # 基本台股
    ("6770", True),
    ("00981A", True),     # ETF 代號
    ("00988A", True),
    ("123", False),       # 太短
    ("1234567", False),   # 太長
    ("NVDA", False),      # 美股
    ("AAPL", False),
    ("TSLA", False),
    ("GOOGL", False),
    ("", False),
])
def test_tw_sid_regex_filter(sid, valid):
    """過濾 ETF 持股中混入的美股代號（如 NVDA），避免污染分數計算。"""
    assert bool(_TW_SID_RE.match(sid)) is valid


# ── SECTOR_STOCKS["其他"] ─────────────────────────────────────────

ACTIVE_ETF_IN_TRADING = ["00988A.TW", "00990A.TW", "00981A.TW",
                        "00982A.TW", "00985A.TW", "00980A.TW"]


def test_sector_stocks_other_includes_6_active_etfs():
    """6 檔保守版主動 ETF（含 2 檔美股 ETF）在交易池。"""
    other = SECTOR_STOCKS.get("其他", {})
    for code in ACTIVE_ETF_IN_TRADING:
        assert code in other, f"{code} 應在 SECTOR_STOCKS['其他']"


def test_sector_stocks_other_active_etfs_have_names():
    other = SECTOR_STOCKS.get("其他", {})
    for code in ACTIVE_ETF_IN_TRADING:
        name = other.get(code, "")
        assert name and len(name) >= 4, f"{code} 名稱缺或太短：{name!r}"


def test_existing_other_etfs_preserved():
    """既有 0050 / 0056 / 00878 / 00919 不該被刪掉。"""
    other = SECTOR_STOCKS.get("其他", {})
    for code in ["0050.TW", "0056.TW", "00878.TW", "00919.TW"]:
        assert code in other, f"既有 ETF {code} 不該被移除"


def test_us_etfs_in_trading_but_not_in_beat():
    """美股 ETF (00988A / 00990A) 在交易池 OK、但不該進 BEAT_ETFS 加分清單。"""
    other = SECTOR_STOCKS.get("其他", {})
    beat_codes = {e["code"] for e in BEAT_ETFS}
    for code in ["00988A.TW", "00990A.TW"]:
        assert code in other, f"{code} 應在交易池"
        assert code.replace(".TW", "") not in beat_codes, \
            f"{code} 不該在 BEAT_ETFS（美股 ETF）"


def test_no_loser_etfs_in_sector_stocks():
    """明確輸大盤的 ETF（如 00403A / 00983A）不該在交易池。"""
    other = SECTOR_STOCKS.get("其他", {})
    losers = ["00403A.TW", "00983A.TW", "00989A.TW", "00986A.TW"]
    for code in losers:
        assert code not in other, f"輸大盤 ETF {code} 不該在交易池"
