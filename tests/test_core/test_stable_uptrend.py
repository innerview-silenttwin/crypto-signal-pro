"""長線標的 (STABLE_UPTREND_SYMBOLS) 名單守門測試。

背景：2026-06-16 用戶確認「穩定向上」候選 = 2330.TW + 0050.TW，作為未來 dip-buy
邏輯的白名單。目前僅 metadata 標記、沒接信號層。

破壞條件（半自動審視，未實作）：
- 跌破 200MA 且 200MA 翻空
- 從歷史高點回檔 ≥ 30%
- 連 2 季 EPS 衰退
任一觸發 → telegram 通知人工 review 是否移出清單。

詳見 memory/project_stable_uptrend_universe.md。
"""

from sector_trader import STABLE_UPTREND_SYMBOLS


def test_stable_uptrend_contains_expected_symbols():
    """目前名單只應有 2330.TW + 0050.TW；增刪都要動測試。"""
    assert STABLE_UPTREND_SYMBOLS == {"2330.TW", "0050.TW"}


def test_stable_uptrend_uses_tw_suffix():
    """所有 key 必須帶 .TW 後綴（對齊 production state["stocks"] 格式）。"""
    for s in STABLE_UPTREND_SYMBOLS:
        assert s.endswith(".TW"), f"{s!r} 缺 .TW 後綴；後續邏輯比對會 miss"


def test_stable_uptrend_excludes_high_dividend_etfs():
    """高股息 ETF 屬「穩定收息」不是「穩定向上」，明示不在名單。

    防止未來有人想加 0056 / 00878 / 00919 等高股息進來時誤把含意混淆。
    """
    high_div_etfs = {"0056.TW", "00878.TW", "00919.TW", "00713.TW"}
    assert STABLE_UPTREND_SYMBOLS.isdisjoint(high_div_etfs), \
        "高股息 ETF 不該列為 stable_uptrend；要加要先重新定義語意"
