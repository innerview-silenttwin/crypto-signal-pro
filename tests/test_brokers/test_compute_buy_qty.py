"""compute_buy_qty_pure — 純函式測試（不依賴 broker / state）

規則：
  - price > 100  → 零股，floor(target / price)
  - price ≤ 100  → 整張，floor(target / (price × 1000)) × 1000
  - target = min(max_order, available_cash)
"""
from sector_trader import compute_buy_qty_pure


# ── price ≤ 100：整張路徑 ──

def test_cheap_stock_buys_multiple_lots():
    """12 元股票，100k 上限 → 應該買 8 張 (96k)"""
    qty = compute_buy_qty_pure(price=12.0, max_order=100_000, available_cash=10_000_000)
    assert qty == 8000  # 8 lots × 1000 shares


def test_35_yuan_etf_buys_2_lots():
    """0050 35 元 → 2 張 (70k)，不補零股到 100k"""
    qty = compute_buy_qty_pure(price=35.0, max_order=100_000, available_cash=10_000_000)
    assert qty == 2000


def test_60_yuan_buys_1_lot():
    """60 元 → 1 張 (60k)，不補零股"""
    qty = compute_buy_qty_pure(price=60.0, max_order=100_000, available_cash=10_000_000)
    assert qty == 1000


def test_100_yuan_boundary_buys_1_lot():
    """剛好 100 元 → 1 張 (100k)，走整張路徑"""
    qty = compute_buy_qty_pure(price=100.0, max_order=100_000, available_cash=10_000_000)
    assert qty == 1000


# ── price > 100：零股路徑 ──

def test_101_yuan_odd_lot():
    """101 元 → 零股，target 100k / 101 = 990 股"""
    qty = compute_buy_qty_pure(price=101.0, max_order=100_000, available_cash=10_000_000)
    assert qty == 990


def test_tsmc_1845_odd_lot():
    """TSMC 1845 元 → 零股 54 股 (99,630)"""
    qty = compute_buy_qty_pure(price=1845.0, max_order=100_000, available_cash=10_000_000)
    assert qty == 54


def test_novatek_293_odd_lot():
    """聯詠 293 元 → 零股 341 股 (99,913)"""
    qty = compute_buy_qty_pure(price=293.0, max_order=100_000, available_cash=10_000_000)
    assert qty == 341


# ── 帳戶可用現金限制 ──

def test_low_cash_reduces_qty():
    """available_cash 50k 比 max_order 100k 低 → 用 50k 算"""
    # 60 元 1 張 = 60k，但只有 50k → 0 張
    qty = compute_buy_qty_pure(price=60.0, max_order=100_000, available_cash=50_000)
    assert qty == 0  # 一張 60k 都買不到


def test_low_cash_buys_what_it_can():
    """available_cash 50k，35 元 → 1 張 (35k)"""
    qty = compute_buy_qty_pure(price=35.0, max_order=100_000, available_cash=50_000)
    assert qty == 1000


def test_low_cash_odd_lot():
    """available_cash 30k，TSMC 1845 → 16 股 (29.5k)"""
    qty = compute_buy_qty_pure(price=1845.0, max_order=100_000, available_cash=30_000)
    assert qty == 16


# ── 邊界 ──

def test_zero_price_returns_zero():
    assert compute_buy_qty_pure(price=0, max_order=100_000, available_cash=100_000) == 0


def test_negative_price_returns_zero():
    assert compute_buy_qty_pure(price=-1, max_order=100_000, available_cash=100_000) == 0


def test_zero_cash_returns_zero():
    assert compute_buy_qty_pure(price=60, max_order=100_000, available_cash=0) == 0


def test_target_capped_by_max_order():
    """available_cash 1,000,000 但 max_order 100,000 → 仍只買 8 張 12 元股票"""
    qty = compute_buy_qty_pure(price=12.0, max_order=100_000, available_cash=1_000_000)
    assert qty == 8000  # 不會買 83 張 (83 × 12000 = 996k)
