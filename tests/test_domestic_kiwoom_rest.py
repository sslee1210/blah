from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from core.domestic_kiwoom_rest import (
    KOREA,
    DomesticKiwoomRestClient,
    _domestic_daily_frame,
    _domestic_minute_frame,
    completed_domestic_daily_bars,
    completed_domestic_minute_bars,
    domestic_daily_frame_is_current,
    is_supported_domestic_stock,
)
from core.kiwoom_rest import KiwoomRestError, StockInfo


def test_domestic_daily_frame_preserves_leading_code_independent_ohlcv_and_krw_turnover() -> None:
    rows = [
        {
            "dt": "20260903",
            "open_pric": "+10000",
            "high_pric": "+11000",
            "low_pric": "-9500",
            "cur_prc": "+10500",
            "trde_qty": "2000",
            "trde_prica": "21",
        }
    ]
    frame = _domestic_daily_frame(rows)
    assert frame.iloc[0]["open"] == 10000
    assert frame.iloc[0]["close"] == 10500
    assert frame.iloc[0]["trade_value"] == 21_000_000


def test_in_progress_domestic_daily_bar_is_removed_before_1530() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000.0, 1100.0],
            "trade_value": [101000.0, 112200.0],
        },
        index=pd.to_datetime(["2026-09-03", "2026-09-04"]),
    )
    before_close = datetime(2026, 9, 4, 10, 30, tzinfo=KOREA)
    after_close = datetime(2026, 9, 4, 15, 31, tzinfo=KOREA)
    assert completed_domestic_daily_bars(frame, now=before_close).index[-1].date().isoformat() == "2026-09-03"
    assert completed_domestic_daily_bars(frame, now=after_close).index[-1].date().isoformat() == "2026-09-04"


def test_domestic_master_filter_uses_market_type_and_excludes_preferred_spac_warning() -> None:
    common = StockInfo("005930", "KOSPI", "삼성전자", "", "전기전자", False)
    preferred = StockInfo("005935", "KOSPI", "삼성전자우", "", "전기전자", False)
    spac = StockInfo("1234A0", "KOSDAQ", "테스트제1호스팩", "", "금융", False)
    alpha_code = StockInfo("0001A0", "KOSDAQ", "테스트일반주", "", "제조", False)
    assert is_supported_domestic_stock(common, market_name="거래소")
    assert not is_supported_domestic_stock(common, market_name="ETF")
    assert not is_supported_domestic_stock(preferred, market_name="거래소")
    assert not is_supported_domestic_stock(spac, market_name="코스닥")
    assert is_supported_domestic_stock(alpha_code, market_name="코스닥")
    assert not is_supported_domestic_stock(common, market_name="거래소", order_warning="3")


def test_trading_value_rank_requests_krx_and_management_exclusion(monkeypatch) -> None:
    client = DomesticKiwoomRestClient.__new__(DomesticKiwoomRestClient)
    captured = {}

    def fake_paged(api_id, path, body, **kwargs):
        captured.update(body)
        return []

    monkeypatch.setattr(client, "paged", fake_paged)
    client.trading_value_top("001")
    assert captured["mrkt_tp"] == "001"
    assert captured["mang_stk_incls"] == "1"
    assert captured["stex_tp"] == "1"


def test_current_60m_bucket_is_removed_until_its_end() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-09-04 09:00", tz=KOREA),
            pd.Timestamp("2026-09-04 10:00", tz=KOREA),
        ]
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1000.0, 500.0],
            "trade_value": [101000.0, 51000.0],
        },
        index=index,
    )
    now = datetime(2026, 9, 4, 10, 31, tzinfo=KOREA)
    completed = completed_domestic_minute_bars(frame, interval_minutes=60, now=now)
    assert list(completed.index.hour) == [9]


def test_explicit_etf_metadata_is_excluded_without_an_etf_name_suffix() -> None:
    etf = StockInfo("069500", "KOSPI", "KODEX 200", "", "미분류", True)
    assert not is_supported_domestic_stock(etf)


@pytest.mark.parametrize("parser,time_field,time_value", [
    (_domestic_daily_frame, "dt", "20260904"),
    (_domestic_minute_frame, "cntr_tm", "20260904100000"),
])
@pytest.mark.parametrize("field,value", [
    ("open_pric", "invalid"), ("cur_prc", "0"), ("trde_qty", "inf"),
])
def test_malformed_price_bars_fail_instead_of_silently_shortening_history(
    parser, time_field, time_value, field, value
) -> None:
    row = {
        time_field: time_value,
        "open_pric": "10000",
        "high_pric": "11000",
        "low_pric": "9500",
        "cur_prc": "10500",
        "trde_qty": "2000",
    }
    row[field] = value
    with pytest.raises(KiwoomRestError, match="OHLCV"):
        parser([row])


def test_daily_freshness_rejects_an_unfinished_today_bar() -> None:
    frame = pd.DataFrame({"close": [100.0]}, index=pd.to_datetime(["2026-09-04"]))
    assert not domestic_daily_frame_is_current(
        frame, now=datetime(2026, 9, 4, 10, 30, tzinfo=KOREA)
    )


def test_missing_volume_stays_unknown_instead_of_becoming_zero() -> None:
    frame = _domestic_daily_frame([{
        "dt": "20260904", "open_pric": "10000", "high_pric": "11000",
        "low_pric": "9500", "cur_prc": "10500", "trde_qty": "",
    }])
    assert pd.isna(frame.iloc[0]["volume"])
    assert pd.isna(frame.iloc[0]["trade_value"])
