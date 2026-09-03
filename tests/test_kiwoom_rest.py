from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from core.credentials import KiwoomCredentials
from core.kiwoom_rest import (
    KiwoomRestClient,
    KiwoomRestError,
    StockInfo,
    _daily_frame,
    _minute_frame,
    completed_daily_bars,
    latest_completed_us_weekday,
    number,
    regular_session_minute_bars,
)


US_EASTERN = ZoneInfo("America/New_York")


def test_signed_kiwoom_prices_are_treated_as_absolute_prices() -> None:
    assert number("-201.4500", absolute=True) == 201.45
    assert number("+1,234.5000", absolute=True) == 1234.5
    assert number("", absolute=True) is None


def test_daily_response_is_sorted_and_deduplicated() -> None:
    rows = [
        {"dt": "20260103", "open_pric": "-10", "high_pric": "11", "low_pric": "9", "cur_prc": "10.5", "acc_trde_qty": "100"},
        {"dt": "20260102", "open_pric": "9", "high_pric": "10", "low_pric": "8", "cur_prc": "9.5", "acc_trde_qty": "90"},
        {"dt": "20260103", "open_pric": "10", "high_pric": "11", "low_pric": "9", "cur_prc": "10.5", "acc_trde_qty": "100"},
    ]
    frame = _daily_frame(rows)
    assert list(frame.index.strftime("%Y%m%d")) == ["20260102", "20260103"]
    assert frame.iloc[-1]["open"] == 10
    assert frame.iloc[-1]["trade_value"] == 1050


def test_minute_response_uses_new_york_timezone() -> None:
    rows = [
        {"cntr_tm": "20260102103000", "open_pric": "10", "high_pric": "11", "low_pric": "9", "cur_prc": "10.5", "trde_qty": "100"}
    ]
    frame = _minute_frame(rows)
    assert len(frame) == 1
    assert str(frame.index.tz) == "America/New_York"


def test_minute_response_parses_kiwoom_overnight_hours_above_23() -> None:
    rows = [
        {
            "cntr_tm": "20260902260000",
            "bus_dt": "20260902",
            "open_pric": "10",
            "high_pric": "11",
            "low_pric": "9",
            "cur_prc": "10.5",
            "trde_qty": "100",
        }
    ]
    frame = _minute_frame(rows)
    assert frame.index[0].isoformat() == "2026-09-03T02:00:00-04:00"


def test_regular_session_filter_excludes_pre_and_after_hours() -> None:
    rows = [
        {
            "cntr_tm": f"20260902{hour:02d}0000",
            "bus_dt": "20260902",
            "open_pric": "10",
            "high_pric": "11",
            "low_pric": "9",
            "cur_prc": "10.5",
            "trde_qty": "100",
        }
        for hour in (4, 9, 10, 16, 17, 26)
    ]
    filtered = regular_session_minute_bars(_minute_frame(rows))
    assert list(filtered.index.hour) == [9, 10, 16]


def test_paging_passes_server_continuation_key(monkeypatch) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    calls = []

    def fake_request(api_id, path, body, *, cont_yn="N", next_key=""):
        calls.append((cont_yn, next_key))
        if len(calls) == 1:
            return {"result_list": [{"id": 1}], "return_code": 0}, {"cont-yn": "Y", "next-key": "NEXT"}
        return {"result_list": [{"id": 2}], "return_code": 0}, {"cont-yn": "N", "next-key": ""}

    monkeypatch.setattr(client, "request", fake_request)
    rows = client.paged("usa06012", "/api/us/chart", {}, list_key="result_list")
    assert rows == [{"id": 1}, {"id": 2}]
    assert calls == [("N", ""), ("Y", "NEXT")]


def test_stock_info_requires_a_valid_exchange_without_calling_network(monkeypatch) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: pytest.fail("network called"))
    with pytest.raises(KiwoomRestError, match="거래소 코드가 필요"):
        client.stock_info("AAPL", "")


def _daily_rows_ending(end, periods: int = 100):
    return [
        {
            "dt": stamp.strftime("%Y%m%d"),
            "open_pric": "100",
            "high_pric": "102",
            "low_pric": "99",
            "cur_prc": "101",
            "acc_trde_qty": "1000000",
        }
        for stamp in pd.bdate_range(end=end, periods=periods)
    ]


def test_daily_request_uses_today_as_backward_anchor(monkeypatch) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    captured = {}
    now = datetime.now(US_EASTERN)
    rows = _daily_rows_ending(latest_completed_us_weekday(now))

    def fake_paged(api_id, path, body, **kwargs):
        captured.update(body)
        return rows

    monkeypatch.setattr(client, "paged", fake_paged)
    frame = client.daily_bars(stock, calendar_days=1000, max_rows=100)
    assert captured["strt_dt"] == now.strftime("%Y%m%d")
    assert len(frame) == 100


def test_daily_request_rejects_an_old_api_window(monkeypatch) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    rows = _daily_rows_ending("2023-12-08")
    monkeypatch.setattr(client, "paged", lambda *args, **kwargs: rows)
    with pytest.raises(KiwoomRestError, match="최신 데이터가 아니므로"):
        client.daily_bars(stock, calendar_days=2000, max_rows=100)


def test_in_progress_daily_bar_is_removed_before_analysis() -> None:
    before_close = datetime(2026, 9, 3, 15, 30, tzinfo=US_EASTERN)
    after_close = datetime(2026, 9, 3, 16, 1, tzinfo=US_EASTERN)
    frame = _daily_frame(_daily_rows_ending("2026-09-03", periods=100))
    assert completed_daily_bars(frame, now=before_close).index[-1].date().isoformat() == "2026-09-02"
    assert completed_daily_bars(frame, now=after_close).index[-1].date().isoformat() == "2026-09-03"
