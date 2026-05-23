"""router 拆檔的安全網：驗每個已搬出去的 endpoint 仍在 app.routes 註冊。

每搬一個 router 就在 EXPECTED_ROUTES 加新條目，pytest 自動驗證沒漏路。
**只驗 path + method 仍存在**，不打 endpoint 內部、不觸發業務邏輯。
"""

import pytest


# 預期所有 router 拆檔後仍在 app 上的 (method, path) 配對。
# 拆新 router 時：把對應 endpoint 加進來。
EXPECTED_ROUTES = [
    # api/trading.py — A1
    ("POST", "/api/trading/toggle"),
    ("GET",  "/api/trading/status"),
    ("GET",  "/api/trading/history"),
    ("GET",  "/api/trading/symbols"),
    ("POST", "/api/trading/symbols/add"),
    ("POST", "/api/trading/symbols/remove"),

    # api/btc_trading.py — A2
    ("GET",  "/api/btc-trading/status"),
    ("POST", "/api/btc-trading/toggle"),
    ("POST", "/api/btc-trading/run-once"),
    ("GET",  "/api/btc-trading/history"),
    ("GET",  "/api/btc-trading/equity-curve"),
    ("GET",  "/api/btc-trading/flow-info"),

    # api/notifications.py — A2 (分家自 btc_trading)
    ("POST", "/api/daily-report/send"),
]


@pytest.fixture(scope="module")
def app():
    from main import app as fastapi_app
    return fastapi_app


@pytest.fixture(scope="module")
def registered_routes(app):
    """{(method, path): route} 配對。"""
    mapping = {}
    for route in app.routes:
        if not hasattr(route, "path") or not hasattr(route, "methods"):
            continue
        for m in route.methods:
            if m == "HEAD":
                continue
            mapping[(m, route.path)] = route
    return mapping


@pytest.mark.parametrize("method,path", EXPECTED_ROUTES)
def test_expected_route_registered(registered_routes, method, path):
    """每個拆出去的 endpoint 仍要能在 app.routes 找到，URL/method 完全一致。"""
    assert (method, path) in registered_routes, (
        f"Route 消失了：{method} {path}\n"
        f"已註冊的相關路徑：{sorted(p for _, p in registered_routes if p.startswith(path[:15]))}"
    )
