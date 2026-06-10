"""永豐 quote 異常 fallback 計數器（6/4 deferred #1）。

驗 module-level 計數器：累計 / 未知 reason 忽略 / 按台北日期跨日歸零。
import sinopac_provider 安全——shioaji 只在 SinopacQuoteProvider.__init__ 內 import，
module top 不依賴它。
"""

import pytest

import quote_provider.sinopac_provider as sp


@pytest.fixture(autouse=True)
def reset_stats():
    """每個 test 前把 module-level 計數歸零，避免互相污染。"""
    sp._fallback_stats.update(
        {"date": None, "contract_not_found": 0, "kbars_failed": 0, "kbars_empty": 0}
    )
    yield


def test_record_and_get_total():
    sp._record_fallback("kbars_failed")
    sp._record_fallback("kbars_failed")
    sp._record_fallback("contract_not_found")
    s = sp.get_fallback_stats()
    assert s["kbars_failed"] == 2
    assert s["contract_not_found"] == 1
    assert s["kbars_empty"] == 0
    assert s["total"] == 3


def test_unknown_reason_ignored():
    """非法 reason 不計數（防呆，避免之後改 caller 拼錯字默默累計到不存在的桶）。"""
    sp._record_fallback("bogus")
    assert sp.get_fallback_stats()["total"] == 0


def test_get_stats_schema():
    snap = sp.get_fallback_stats()
    assert set(snap) == {"date", "contract_not_found", "kbars_failed", "kbars_empty", "total"}
    assert snap["total"] == 0


def test_date_rollover_resets():
    sp._record_fallback("kbars_failed")
    assert sp.get_fallback_stats()["total"] == 1

    # 模擬跨日：把記錄日期改成過去某天
    sp._fallback_stats["date"] = "2000-01-01"
    # 讀取時若日期非今日 → 回零（不污染新的一天）
    assert sp.get_fallback_stats()["total"] == 0

    # 新的一次 record 會先 reset 再 +1，舊計數歸零
    sp._record_fallback("kbars_empty")
    s = sp.get_fallback_stats()
    assert s["total"] == 1
    assert s["kbars_empty"] == 1
    assert s["kbars_failed"] == 0
