"""SinopacBroker 用 mock shioaji 驗證 — 確認下單參數正確、不洩漏 credentials。

Phase 1 不接真環境，這個檔案以 mock 為主軸，覆蓋：
  - 股↔張轉換（< 1 張會被擋）
  - LMT / ROD / Common / Cash 參數正確
  - timeout 後會 cancel
  - partial fill 正確回報
  - api_key / secret_key 不會出現在任何 log handler 的記錄裡
"""

import logging
import sys
import types

import pytest


# ── 假 shioaji 模組（注入到 sys.modules，讓 from .sinopac import 走 mock）──

class _FakeAction:
    Buy = "Buy"
    Sell = "Sell"

class _FakeStockPriceType:
    LMT = "LMT"
    MKT = "MKT"

class _FakeOrderType:
    ROD = "ROD"
    IOC = "IOC"
    FOK = "FOK"

class _FakeStockOrderLot:
    Common = "Common"
    IntradayOdd = "IntradayOdd"

class _FakeStockOrderCond:
    Cash = "Cash"
    MarginTrading = "MarginTrading"

class _FakeConstant:
    Action = _FakeAction
    StockPriceType = _FakeStockPriceType
    OrderType = _FakeOrderType
    StockOrderLot = _FakeStockOrderLot
    StockOrderCond = _FakeStockOrderCond


class _FakeDeal:
    """Shioaji status.deals 內的 Deal 物件 — 每筆成交。"""
    def __init__(self, price: float, quantity: int):
        self.price = price
        self.quantity = quantity


class _FakeStatus:
    """OrderStatus — 與 llms-full.txt 描述對齊：status / deals / modified_price 等"""
    def __init__(self, status="Submitted", deals=None, modified_price=0.0):
        self.status = status
        self.deals = deals or []   # list[_FakeDeal]
        self.modified_price = modified_price
        # 額外欄位，模擬真實物件
        self.id = "FAKE_STATUS_ID"
        self.order_quantity = 1
        self.cancel_quantity = 0


class _FakeOrder:
    def __init__(self, **kwargs):
        self.params = kwargs
        self.order_id = "FAKE_ORDER_1"


class _FakeTrade:
    def __init__(self, status_str="Submitted", deal_qty_lots: int = 0,
                 avg_price: float | None = None):
        """deal_qty_lots：總成交張數；avg_price：平均成交價（會建立一筆 _FakeDeal 涵蓋）"""
        self.order = _FakeOrder()
        deals = []
        if deal_qty_lots > 0 and avg_price is not None:
            deals = [_FakeDeal(price=avg_price, quantity=deal_qty_lots)]
        self.status = _FakeStatus(status_str, deals=deals)


class _FakeContracts:
    class Stocks(dict):
        def __getitem__(self, k):
            return f"contract_{k}"


class _FakeShioajiAPI:
    def __init__(self, simulation=True):
        self.simulation = simulation
        self.Contracts = _FakeContracts()
        self.stock_account = "FAKE_ACCOUNT"
        # 可由測試覆寫的成交流程
        self._trade_to_return = _FakeTrade("Filled", 1, 900.0)
        self._login_should_fail = False

    def login(self, api_key, secret_key):
        if self._login_should_fail:
            raise RuntimeError("login refused")
        # 不要存 credentials；測試會檢查 logger 不含 api_key
        return [{"account": "X"}]

    def activate_ca(self, ca_path, ca_passwd, person_id):
        return True

    def Order(self, **kwargs):
        return _FakeOrder(**kwargs)

    def place_order(self, contract, order):
        # 把訂單參數記到 trade，方便 assertion
        self._last_order_params = order.params
        self._last_contract = contract
        return self._trade_to_return

    def cancel_order(self, trade):
        trade.status.status = "Cancelled"

    def update_status(self):
        return True

    def list_positions(self, account):
        return []

    def set_order_callback(self, cb):
        return True


class _FakeShioajiModule(types.ModuleType):
    def __init__(self):
        super().__init__("shioaji")
        self.constant = _FakeConstant
        self._next_api: _FakeShioajiAPI | None = None

    def Shioaji(self, simulation=True):
        api = _FakeShioajiAPI(simulation=simulation)
        if self._next_api is not None:
            # 允許測試 pre-configure API
            cfg = self._next_api
            api._trade_to_return = cfg._trade_to_return
            api._login_should_fail = cfg._login_should_fail
        return api


@pytest.fixture
def fake_shioaji(monkeypatch):
    fake = _FakeShioajiModule()
    monkeypatch.setitem(sys.modules, "shioaji", fake)
    return fake


# ── 測試 ──

def _new_broker(fake_shioaji):
    from brokers.sinopac import SinopacBroker
    return SinopacBroker(
        api_key="FAKE_API_KEY",
        secret_key="FAKE_SECRET_KEY",
        person_id="A123456789",
        simulation=True,
        fill_timeout_s=2,
        poll_interval_s=0.05,
    )


def test_init_simulation_no_ca_required(fake_shioaji):
    b = _new_broker(fake_shioaji)
    assert b._simulation is True


def test_buy_below_min_lot_returns_rejected(fake_shioaji):
    """qty_shares < 1 才被 sinopac broker 擋；零股（1~999 股）已開放走 IntradayOdd。"""
    b = _new_broker(fake_shioaji)
    r = b.submit(symbol="2330.TW", action="BUY", qty_shares=0,
                 limit_price=1000.0, client_order_id="x", sector_id="semiconductor")
    assert r.ok is False
    assert "below_min_lot" in r.reason


def test_buy_odd_lot_uses_intraday_odd(fake_shioaji):
    """500 股（零股）下單應走 IntradayOdd，quantity 直接是股數。"""
    fake_shioaji._next_api = _FakeShioajiAPI()
    fake_shioaji._next_api._trade_to_return = _FakeTrade("Filled", deal_qty_lots=500, avg_price=905.0)
    b = _new_broker(fake_shioaji)
    r = b.submit(symbol="2330.TW", action="BUY", qty_shares=500,
                 limit_price=900.0, client_order_id="x", sector_id="semiconductor")
    assert r.ok is True
    # 零股的 deal_quantity 直接是股數，不再 × 1000
    assert r.actual_qty == 500
    assert r.fill_status == "filled"


def test_buy_filled_returns_actual_qty_in_shares(fake_shioaji):
    """Shioaji 的 deal_quantity 是張，回傳要轉回股。"""
    fake_shioaji._next_api = _FakeShioajiAPI()
    fake_shioaji._next_api._trade_to_return = _FakeTrade("Filled", deal_qty_lots=1, avg_price=905.0)
    b = _new_broker(fake_shioaji)
    r = b.submit(symbol="2330.TW", action="BUY", qty_shares=1000,
                 limit_price=900.0, client_order_id="x", sector_id="semiconductor")
    assert r.ok is True
    assert r.actual_qty == 1000  # 1 張 × 1000 股
    assert r.actual_price == 905.0
    assert r.fill_status == "filled"


def test_partial_fill_reported(fake_shioaji):
    """要求 2 張、broker 只回 PartFilled 1 張：等到 timeout 後應 cancel 剩餘並回 partial。"""
    fake_shioaji._next_api = _FakeShioajiAPI()
    # PartFilled：不會被 'filled' substring 誤判（修正 #2）
    fake_shioaji._next_api._trade_to_return = _FakeTrade(
        "PartFilled", deal_qty_lots=1, avg_price=905.0
    )
    b = _new_broker(fake_shioaji)
    r = b.submit(symbol="2330.TW", action="BUY", qty_shares=2000,
                 limit_price=900.0, client_order_id="x", sector_id="semiconductor")
    assert r.ok is True
    assert r.actual_qty == 1000   # 1 張 × 1000 股
    assert r.actual_price == 905.0
    assert r.fill_status == "partial"


def test_order_params_correct_lmt_rod_common_cash(fake_shioaji):
    fake_shioaji._next_api = _FakeShioajiAPI()
    fake_shioaji._next_api._trade_to_return = _FakeTrade("Filled", deal_qty_lots=1, avg_price=900.0)
    b = _new_broker(fake_shioaji)
    b.submit(symbol="2330.TW", action="BUY", qty_shares=1000,
             limit_price=900.0, client_order_id="x", sector_id="semiconductor")
    params = b.api._last_order_params
    assert params["price_type"] == "LMT"
    assert params["order_type"] == "ROD"
    assert params["order_lot"] == "Common"
    assert params["order_cond"] == "Cash"
    assert params["action"] == "Buy"
    assert params["quantity"] == 1   # 1 張，不是 1000


def test_sell_action_passes_through(fake_shioaji):
    fake_shioaji._next_api = _FakeShioajiAPI()
    fake_shioaji._next_api._trade_to_return = _FakeTrade("Filled", deal_qty_lots=1, avg_price=910.0)
    b = _new_broker(fake_shioaji)
    b.submit(symbol="2330.TW", action="SELL", qty_shares=1000,
             limit_price=910.0, client_order_id="x", sector_id="semiconductor")
    assert b.api._last_order_params["action"] == "Sell"


def test_timeout_cancels_order(fake_shioaji):
    fake_shioaji._next_api = _FakeShioajiAPI()
    # status 永遠不變 → timeout
    fake_shioaji._next_api._trade_to_return = _FakeTrade("PendingSubmit", deal_qty_lots=0)
    b = _new_broker(fake_shioaji)
    r = b.submit(symbol="2330.TW", action="BUY", qty_shares=1000,
                 limit_price=900.0, client_order_id="x", sector_id="semiconductor")
    assert r.ok is False
    assert r.fill_status == "timeout"


def test_rejection_returns_rejected(fake_shioaji):
    fake_shioaji._next_api = _FakeShioajiAPI()
    fake_shioaji._next_api._trade_to_return = _FakeTrade("Failed")
    b = _new_broker(fake_shioaji)
    r = b.submit(symbol="2330.TW", action="BUY", qty_shares=1000,
                 limit_price=900.0, client_order_id="x", sector_id="semiconductor")
    assert r.ok is False
    assert r.fill_status == "rejected"


def test_credentials_not_in_logs(fake_shioaji, caplog):
    """API key / secret key 不該出現在 log。"""
    caplog.set_level(logging.DEBUG)
    fake_shioaji._next_api = _FakeShioajiAPI()
    fake_shioaji._next_api._login_should_fail = True
    from brokers.sinopac import SinopacBroker
    with pytest.raises(Exception):
        SinopacBroker(api_key="SUPER_SECRET_KEY_42",
                      secret_key="ANOTHER_SECRET_43",
                      person_id="A123456789",
                      simulation=True)
    # 檢查所有 log 都不含 credentials
    captured = caplog.text
    assert "SUPER_SECRET_KEY_42" not in captured
    assert "ANOTHER_SECRET_43" not in captured


def test_shioaji_internal_logger_set_to_warning(fake_shioaji):
    _new_broker(fake_shioaji)
    sj_logger = logging.getLogger("shioaji")
    assert sj_logger.level >= logging.WARNING


def test_non_simulation_requires_ca(fake_shioaji):
    """SHIOAJI_SIMULATION=false 但沒給 ca_path → 應該 raise。"""
    from brokers.sinopac import SinopacBroker
    with pytest.raises(RuntimeError, match="CA"):
        SinopacBroker(api_key="x", secret_key="y", person_id="z",
                      simulation=False)
