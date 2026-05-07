"""測試交易時段判斷 — 邊界、週末、節假日、ex-div 凍結。"""

from datetime import datetime
from unittest import mock

import pytz
import pytest

from brokers import market_hours as mh


_TW = pytz.timezone("Asia/Taipei")


def _tw(year, month, day, hour=0, minute=0):
    return _TW.localize(datetime(year, month, day, hour, minute))


# ── 邊界：09:00 / 13:25 / 13:30 / 13:31 ──

@pytest.mark.parametrize("h,m,signal,order", [
    (8, 59, False, False),
    (9, 0, True, True),
    (13, 24, True, True),
    (13, 25, True, True),     # 13:25 含
    (13, 26, True, False),    # 13:26 後不再下新單
    (13, 30, True, False),
    (13, 31, False, False),
])
def test_signal_and_order_window_boundaries(h, m, signal, order):
    n = _tw(2026, 5, 6, h, m)  # 週三
    assert mh.is_signal_window(n) is signal, f"signal_window @ {h:02d}:{m:02d}"
    assert mh.is_orderable_now(n) is order, f"orderable_now @ {h:02d}:{m:02d}"


def test_data_capture_window_extends_to_1415():
    assert mh.is_data_capture_window(_tw(2026, 5, 6, 14, 14)) is True
    assert mh.is_data_capture_window(_tw(2026, 5, 6, 14, 15)) is True
    assert mh.is_data_capture_window(_tw(2026, 5, 6, 14, 16)) is False


# ── 週末 ──

def test_weekend_blocks_all_windows():
    sat = _tw(2026, 5, 9, 10, 0)   # 週六
    sun = _tw(2026, 5, 10, 10, 0)  # 週日
    for n in (sat, sun):
        assert mh.is_signal_window(n) is False
        assert mh.is_orderable_now(n) is False
        assert mh.is_data_capture_window(n) is False


# ── 節假日 ──

def test_holiday_blocks_window(monkeypatch):
    target = "2026-05-06"
    # 用 monkeypatch 取代 _holidays 的 cache
    fake_holidays = frozenset({target})
    monkeypatch.setattr(mh, "_holidays", lambda: fake_holidays)
    n = _tw(2026, 5, 6, 10, 0)  # 週三 10:00
    assert mh.is_signal_window(n) is False
    assert mh.is_orderable_now(n) is False
    assert mh.is_data_capture_window(n) is False


# ── ex-div 凍結 ──

def test_ex_div_freeze_covers_d_and_d_plus_1(monkeypatch):
    # 假行事曆：2330.TW 除權息 2026-06-19
    fake_cal = {"2330.TW": frozenset({"2026-06-19"})}
    monkeypatch.setattr(mh, "_ex_div_calendar", lambda: fake_cal)

    assert mh.is_within_ex_div_freeze("2330.TW", "2026-06-18") is False  # D-1
    assert mh.is_within_ex_div_freeze("2330.TW", "2026-06-19") is True   # D
    assert mh.is_within_ex_div_freeze("2330.TW", "2026-06-20") is True   # D+1
    assert mh.is_within_ex_div_freeze("2330.TW", "2026-06-21") is False  # D+2

    # 別的標的不影響
    assert mh.is_within_ex_div_freeze("2454.TW", "2026-06-19") is False
