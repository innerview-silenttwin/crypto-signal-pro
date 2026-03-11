"""
UI Test: 時間框架切換
測試 1D / 4H / 1H 按鈕能正確載入走勢圖資料
"""
import re
import pytest
from playwright.sync_api import Page, expect


BASE_URL = "http://localhost:8000/dashboard/"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "ignore_https_errors": True}


def wait_for_chart_data(page: Page, timeframe: str, timeout: int = 10000):
    """等待圖表 API 回應完成"""
    with page.expect_response(
        lambda r: "/api/chart" in r.url and f"timeframe={timeframe}" in r.url,
        timeout=timeout
    ) as response_info:
        yield response_info.value


class TestTimeframeSwitch:

    def test_page_loads(self, page: Page):
        """頁面正常載入"""
        page.goto(BASE_URL)
        expect(page).to_have_title(re.compile(r"CryptoSignal Pro"))
        expect(page.locator("#tv-chart")).to_be_visible()

    def test_default_timeframe_is_1d(self, page: Page):
        """預設選中 1D 按鈕"""
        page.goto(BASE_URL)
        btn_1d = page.locator(".chart-toggles button", has_text="1D")
        expect(btn_1d).to_have_class(re.compile(r"active"))

    def test_1d_chart_loads_data(self, page: Page):
        """1D 圖表載入資料"""
        api_called = []

        def on_response(response):
            if "/api/chart" in response.url and "timeframe=1d" in response.url:
                api_called.append(response.status)

        page.on("response", on_response)
        page.goto(BASE_URL)
        page.wait_for_timeout(5000)  # 等待初始載入

        assert len(api_called) > 0, "1D 圖表 API 未被呼叫"
        assert api_called[0] == 200, f"1D 圖表 API 回應異常: {api_called[0]}"

    def test_switch_to_4h(self, page: Page):
        """點擊 4H 按鈕後，API 回傳資料且按鈕變為 active"""
        page.goto(BASE_URL)
        page.wait_for_timeout(3000)

        api_result = {}

        def on_response(response):
            if "/api/chart" in response.url and "timeframe=4h" in response.url:
                api_result["status"] = response.status
                try:
                    api_result["count"] = len(response.json())
                except Exception:
                    api_result["count"] = -1

        page.on("response", on_response)

        btn_4h = page.locator(".chart-toggles button", has_text="4H")
        btn_4h.click()
        page.wait_for_timeout(5000)

        assert "status" in api_result, "切換 4H 後未呼叫 /api/chart"
        assert api_result["status"] == 200, f"4H API 回應狀態異常: {api_result.get('status')}"
        assert api_result["count"] > 0, f"4H 圖表資料為空 (count={api_result.get('count')})"

        expect(btn_4h).to_have_class(re.compile(r"active"))

    def test_switch_to_1h(self, page: Page):
        """點擊 1H 按鈕後，API 回傳資料且按鈕變為 active"""
        page.goto(BASE_URL)
        page.wait_for_timeout(3000)

        api_result = {}

        def on_response(response):
            if "/api/chart" in response.url and "timeframe=1h" in response.url:
                api_result["status"] = response.status
                try:
                    api_result["count"] = len(response.json())
                except Exception:
                    api_result["count"] = -1

        page.on("response", on_response)

        btn_1h = page.locator(".chart-toggles button", has_text="1H")
        btn_1h.click()
        page.wait_for_timeout(5000)

        assert "status" in api_result, "切換 1H 後未呼叫 /api/chart"
        assert api_result["status"] == 200, f"1H API 回應狀態異常: {api_result.get('status')}"
        assert api_result["count"] > 0, f"1H 圖表資料為空 (count={api_result.get('count')})"

        expect(btn_1h).to_have_class(re.compile(r"active"))

    def test_switch_sequence(self, page: Page):
        """連續切換 1D → 4H → 1H → 1D，每次都能正確載入"""
        page.goto(BASE_URL)
        page.wait_for_timeout(3000)

        results = {}

        def on_response(response):
            if "/api/chart" in response.url:
                for tf in ["1d", "4h", "1h"]:
                    if f"timeframe={tf}" in response.url:
                        results[tf] = response.status

        page.on("response", on_response)

        for label in ["4H", "1H", "1D"]:
            btn = page.locator(".chart-toggles button", has_text=label)
            btn.click()
            page.wait_for_timeout(3000)

        for tf in ["4h", "1h", "1d"]:
            assert results.get(tf) == 200, f"{tf.upper()} 切換後 API 狀態異常: {results.get(tf)}"
