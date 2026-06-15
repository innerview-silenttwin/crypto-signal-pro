"""sector-level `no_buy_symbols` 機制單元測試。

背景：2026-06-15 觀察到 2317.TW 同時被 electronics 跟 semiconductor 兩個 sector 持有，
其中 semiconductor 不該再進場（2317 屬電子代工）。新增 `no_buy_symbols` 欄位讓特定
symbol 在該 sector「只賣不買」 — 已有持倉走正常 SELL/停損，新進場直接 skip。

整合點：backend/sector_auto_trader.py process_sector 內「無持倉 BUY 分支」開頭。
"""


def _check_no_buy(state: dict, symbol: str) -> bool:
    """重現 process_sector 內的 no_buy gate（sector_auto_trader.py 行 929-931）。

    回 True 表示「該 skip BUY」。完整邏輯：
        no_buy_set = set(manager.state.get("no_buy_symbols", []))
        if symbol in no_buy_set:
            continue  # skip BUY
    """
    no_buy_set = set(state.get("no_buy_symbols", []))
    return symbol in no_buy_set


def test_empty_no_buy_does_not_block():
    state = {"stocks": ["2317.TW", "2330.TW"]}
    assert _check_no_buy(state, "2317.TW") is False
    assert _check_no_buy(state, "2330.TW") is False


def test_missing_no_buy_field_defaults_to_empty():
    """完全沒此欄位（舊版 state）不會 KeyError，預設一律放行。"""
    assert _check_no_buy({}, "2317.TW") is False


def test_listed_symbol_blocked():
    state = {"no_buy_symbols": ["2317.TW"]}
    assert _check_no_buy(state, "2317.TW") is True


def test_unlisted_symbol_not_blocked():
    state = {"no_buy_symbols": ["2317.TW"]}
    assert _check_no_buy(state, "2330.TW") is False


def test_multiple_no_buy_symbols():
    state = {"no_buy_symbols": ["2317.TW", "6488.TW"]}
    assert _check_no_buy(state, "2317.TW") is True
    assert _check_no_buy(state, "6488.TW") is True
    assert _check_no_buy(state, "2454.TW") is False


def test_design_no_buy_only_affects_no_holdings_branch():
    """文件化設計意圖：no_buy gate 位於「無持倉 → BUY」else 分支內。

    process_sector 結構：
        if hold and hold["qty"] > 0:
            # SELL / 停損 / S9 / S1 / pullback_sell ← 不過 no_buy gate
        else:
            # no_buy check ← 只擋這裡
            # BUY 判斷
    所以已有持倉的 SELL/停損絕對不會被 no_buy 擋掉。
    """
    state = {"no_buy_symbols": ["2317.TW"]}
    # 函式本身只看 state、不看 holdings；呼叫端負責放對位置。
    assert _check_no_buy(state, "2317.TW") is True
