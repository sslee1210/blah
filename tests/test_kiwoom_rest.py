from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from core.credentials import KiwoomCredentials
from core.kiwoom_rest import (
    KiwoomRestClient,
    KiwoomRestError,
    OhlcQualityError,
    StockInfo,
    _daily_frame,
    _minute_frame,
    completed_daily_bars,
    latest_completed_us_weekday,
    normalize_kiwoom_us_symbol,
    number,
    completed_us_minute_bars,
    regular_session_minute_bars,
)


US_EASTERN = ZoneInfo("America/New_York")


def test_signed_kiwoom_prices_are_treated_as_absolute_prices() -> None:
    assert number("-201.4500", absolute=True) == 201.45
    assert number("+1,234.5000", absolute=True) == 1234.5
    assert number("", absolute=True) is None


@pytest.mark.parametrize("parser", [_daily_frame, _minute_frame])
@pytest.mark.parametrize("problem", ["date", "missing_price", "invalid_range"])
def test_malformed_us_bars_are_not_silently_removed(parser, problem) -> None:
    row = {
        "dt": "20260902", "cntr_tm": "20260902100000", "bus_dt": "20260902",
        "open_pric": "10", "high_pric": "11", "low_pric": "9", "cur_prc": "10.5",
        "acc_trde_qty": "100", "trde_qty": "100",
    }
    bad = dict(row)
    if problem == "date":
        bad["dt"] = bad["cntr_tm"] = "invalid"
    elif problem == "missing_price":
        bad["cur_prc"] = ""
    else:
        bad["high_pric"] = "1"
    with pytest.raises(KiwoomRestError, match="OHLCV"):
        parser([row, bad])


@pytest.mark.parametrize(
    ("input_symbol", "expected"),
    [
        ("BRK.B", "BRKB"),
        ("BRK-B", "BRKB"),
        ("BRK/B", "BRKB"),
        ("brkb", "BRKB"),
        ("BRK.A", "BRKA"),
        ("AAPL", "AAPL"),
    ],
)
def test_kiwoom_class_share_symbol_normalization_is_explicit(
    input_symbol: str, expected: str
) -> None:
    assert normalize_kiwoom_us_symbol(input_symbol) == expected


def test_large_ohlc_range_violation_is_classified_but_still_blocked() -> None:
    row = {
        "dt": "20260908",
        "open_pric": "100",
        "high_pric": "110",
        "low_pric": "90",
        "cur_prc": "180",
        "acc_trde_qty": "100",
    }

    with pytest.raises(OhlcQualityError) as exc_info:
        _daily_frame([row])

    assert exc_info.value.category == "B:CORPORATE_ACTION_SUSPECTED"


def test_small_ohlc_range_violation_remains_malformed_and_blocked() -> None:
    row = {
        "dt": "20260908",
        "open_pric": "100",
        "high_pric": "110",
        "low_pric": "90",
        "cur_prc": "112",
        "acc_trde_qty": "100",
    }

    with pytest.raises(OhlcQualityError) as exc_info:
        _daily_frame([row])

    assert exc_info.value.category == "C:MALFORMED_OHLC"


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
    filtered = regular_session_minute_bars(_minute_frame(rows), interval_minutes=60)
    assert list(filtered.index.hour) == [10]


def test_current_us_60m_bucket_is_removed_until_complete() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-09-03 10:00", tz=US_EASTERN),
            pd.Timestamp("2026-09-03 11:00", tz=US_EASTERN),
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000.0, 500.0],
        },
        index=index,
    )
    now = datetime(2026, 9, 3, 11, 31, tzinfo=US_EASTERN)
    completed = completed_us_minute_bars(frame, interval_minutes=60, now=now)
    assert list(completed.index.hour) == [10]


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


@pytest.mark.parametrize("value", [None, {}, ["invalid row"]])
def test_paging_rejects_malformed_lists(monkeypatch, value) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: ({"rows": value}, {}))
    with pytest.raises(KiwoomRestError, match="목록 응답 형식"):
        client.paged("usa06012", "/chart", {}, list_key="rows")


@pytest.mark.parametrize("key", ["", "REPEATED"])
def test_paging_does_not_silently_accept_broken_continuation(monkeypatch, key) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    monkeypatch.setattr(
        client, "request", lambda *args, **kwargs: ({"rows": [{"id": 1}]}, {"cont-yn": "Y", "next-key": key})
    )
    with pytest.raises(KiwoomRestError, match="연속 조회 키"):
        client.paged("usa06012", "/chart", {}, list_key="rows")


def test_paging_reports_truncation_but_allows_an_explicit_row_limit(monkeypatch) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    monkeypatch.setattr(
        client, "request", lambda *args, **kwargs: ({"rows": [{"id": 1}]}, {"cont-yn": "Y", "next-key": "NEXT"})
    )
    with pytest.raises(KiwoomRestError, match="페이지 한도"):
        client.paged("usa06012", "/chart", {}, list_key="rows", max_pages=1)
    assert client.paged("usa06012", "/chart", {}, list_key="rows", max_pages=1, max_rows=1) == [{"id": 1}]


def test_us_ranking_page_limit_scales_with_requested_rows(monkeypatch) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    captured = {}

    def fake_paged(api_id, path, body, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(client, "paged", fake_paged)
    client.ranking("usa20550", max_rows=440)

    assert captured["max_rows"] == 440
    assert captured["max_pages"] >= 22


def test_us_and_domestic_clients_share_query_spacing() -> None:
    from core.domestic_kiwoom_rest import DomesticKiwoomRestClient

    credentials = KiwoomCredentials("app", "secret")
    assert KiwoomRestClient(credentials)._limiter is DomesticKiwoomRestClient(credentials)._limiter


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


def test_empty_chart_responses_follow_insufficient_history_handling(monkeypatch) -> None:
    client = KiwoomRestClient(KiwoomCredentials("app", "secret"))
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    monkeypatch.setattr(client, "paged", lambda *args, **kwargs: [])
    with pytest.raises(KiwoomRestError, match="일봉이 0개뿐"):
        client.daily_bars(stock)
    assert client.minute_bars(stock).empty


def test_in_progress_daily_bar_is_removed_before_analysis() -> None:
    before_close = datetime(2026, 9, 3, 15, 30, tzinfo=US_EASTERN)
    after_close = datetime(2026, 9, 3, 16, 1, tzinfo=US_EASTERN)
    frame = _daily_frame(_daily_rows_ending("2026-09-03", periods=100))
    assert completed_daily_bars(frame, now=before_close).index[-1].date().isoformat() == "2026-09-02"
    assert completed_daily_bars(frame, now=after_close).index[-1].date().isoformat() == "2026-09-03"
