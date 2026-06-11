"""logging_config.configure_logging（6/4 deferred #2）。

鎖住三件事，其中第 3 條是安全紅線：
1. root 設在 INFO（讓 logger.info/debug 輸出）。
2. 吵雜第三方 lib 壓到 WARNING。
3. 🔴 urllib3 effective level >= INFO → 不會噴含 Telegram bot token 的 DEBUG 行。
   （notifier.py 把 token 放進 URL；urllib3 只在 DEBUG 印完整 path。）
"""

import logging

import pytest

from logging_config import configure_logging, _NOISY_THIRD_PARTY


@pytest.fixture(autouse=True)
def _restore_logging():
    """測完還原 root level，避免影響其他 test 的 log 捕捉。"""
    root = logging.getLogger()
    saved = root.level
    yield
    root.setLevel(saved)


def test_root_at_info_not_debug():
    configure_logging()
    root = logging.getLogger()
    assert root.level == logging.INFO          # 開 INFO：logger.info 才會輸出
    assert root.level != logging.DEBUG         # 🔴 不可 DEBUG（urllib3 DEBUG 會印 telegram token）


def test_noisy_third_party_pinned_to_warning():
    configure_logging()
    for name in _NOISY_THIRD_PARTY:
        assert logging.getLogger(name).level >= logging.WARNING, name


def test_urllib3_wont_emit_token_bearing_debug():
    """urllib3 含 token 的完整 URL 只在 DEBUG 印；effective level >= INFO 即保證不會。"""
    configure_logging()
    assert logging.getLogger("urllib3").getEffectiveLevel() >= logging.INFO


def test_idempotent():
    """重複呼叫不應改變結果（main 啟動 + 測試各呼叫一次也安全）。"""
    configure_logging()
    configure_logging()
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("yfinance").level >= logging.WARNING
