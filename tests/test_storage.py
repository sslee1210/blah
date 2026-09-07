from __future__ import annotations

from datetime import datetime
import os
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from core.kiwoom_rest import StockInfo, latest_completed_us_weekday
from core.storage import CacheStore
import core.storage as storage_module


US_EASTERN = ZoneInfo("America/New_York")


def _frame_ending(end) -> pd.DataFrame:
    index = pd.bdate_range(end=end, periods=100)
    close = 100.0 + np.arange(len(index))
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1_000_000.0,
            "trade_value": close * 1_000_000.0,
        },
        index=index,
    )


def test_recently_written_but_old_daily_cache_is_not_fresh(tmp_path) -> None:
    cache = CacheStore(tmp_path)
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    cache.save_daily(stock, _frame_ending("2023-12-08"))
    assert cache.load_daily(stock, fresh_only=True) is None
    assert cache.load_daily(stock, fresh_only=False) is not None


def test_recent_current_daily_cache_is_fresh(tmp_path) -> None:
    cache = CacheStore(tmp_path)
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    end = latest_completed_us_weekday(datetime.now(US_EASTERN))
    cache.save_daily(stock, _frame_ending(end))
    assert cache.load_daily(stock, fresh_only=True) is not None


def test_daily_cache_written_before_close_is_refreshed_after_close(tmp_path, monkeypatch) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 3, 16, 1, tzinfo=US_EASTERN)

    monkeypatch.setattr(storage_module, "datetime", Clock)
    cache = CacheStore(tmp_path)
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    cache.save_daily(stock, _frame_ending("2026-09-02"))
    before_close = datetime(2026, 9, 3, 15, 59, tzinfo=US_EASTERN).timestamp()
    os.utime(cache.daily_path(stock), (before_close, before_close))
    assert cache.load_daily(stock, fresh_only=True) is None


def test_minute_cache_preserves_new_york_times_across_dst(tmp_path) -> None:
    cache = CacheStore(tmp_path)
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    frame = _frame_ending("2026-03-09")
    frame.index = (frame.index + pd.Timedelta(hours=10)).tz_localize(US_EASTERN)
    frame.index.freq = None
    frame.index.name = "timestamp"
    cache.save_minute(stock, 60, frame)
    loaded = cache.load_minute(stock, 60)
    pd.testing.assert_frame_equal(loaded, frame)


def test_invalid_cache_dates_are_ignored_instead_of_reaching_analysis(tmp_path) -> None:
    cache = CacheStore(tmp_path)
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    cache.daily_path(stock).write_text("timestamp,open,high,low,close,volume\nbad-date,1,2,1,2,100\n", encoding="utf-8")
    assert cache.load_daily(stock) is None


@pytest.mark.parametrize("column,value", [("high", 0.0), ("close", float("inf")), ("volume", -1.0)])
def test_corrupt_daily_cache_is_a_miss_so_it_can_be_refetched(tmp_path, column, value) -> None:
    cache = CacheStore(tmp_path)
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    frame = _frame_ending(latest_completed_us_weekday(datetime.now(US_EASTERN)))
    frame.loc[frame.index[-1], column] = value
    cache.save_daily(stock, frame)
    assert cache.load_daily(stock, fresh_only=True) is None
