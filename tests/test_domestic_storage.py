from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
import pytest

from core.domestic_kiwoom_rest import KOREA
from core.domestic_storage import DomesticCacheStore, _daily_cache_is_current
from core.kiwoom_rest import StockInfo


def _frame_ending(end: str) -> pd.DataFrame:
    index = pd.bdate_range(end=end, periods=100)
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 1_000_000.0,
            "trade_value": 101_000_000.0,
        },
        index=index,
    )


def test_friday_cache_is_valid_monday_morning_but_forced_to_refresh_after_close(tmp_path) -> None:
    path = tmp_path / "daily.csv"
    path.write_text("cache", encoding="utf-8")
    # 2026-09-04 is Friday and 2026-09-07 is Monday.
    friday_mtime = datetime(2026, 9, 4, 16, 0, tzinfo=KOREA).timestamp()
    os.utime(path, (friday_mtime, friday_mtime))
    frame = _frame_ending("2026-09-04")
    monday_morning = datetime(2026, 9, 7, 9, 30, tzinfo=KOREA)
    monday_after_close = datetime(2026, 9, 7, 15, 31, tzinfo=KOREA)
    assert _daily_cache_is_current(path, frame, now=monday_morning)
    assert not _daily_cache_is_current(path, frame, now=monday_after_close)


def test_post_close_refresh_can_reuse_unchanged_prior_session_on_holiday(tmp_path) -> None:
    path = tmp_path / "daily.csv"
    path.write_text("cache", encoding="utf-8")
    now = datetime(2026, 9, 7, 16, 0, tzinfo=KOREA)
    os.utime(path, (now.timestamp(), now.timestamp()))
    frame = _frame_ending("2026-09-04")
    assert _daily_cache_is_current(path, frame, now=now)


@pytest.mark.parametrize("corruption", ["missing_close", "bad_timestamp", "bad_price"])
def test_corrupt_daily_cache_is_a_cache_miss(tmp_path, corruption) -> None:
    cache = DomesticCacheStore(tmp_path)
    stock = StockInfo("005930", "KOSPI", "삼성전자", "", "전기전자", False)
    frame = _frame_ending("2026-09-04")
    if corruption == "missing_close":
        frame = frame.drop(columns=["close"])
    elif corruption == "bad_timestamp":
        frame.index = ["not-a-date"] * len(frame)
    else:
        frame.loc[frame.index[-1], "high"] = 1.0
    cache.save_daily(stock, frame)
    assert cache.load_daily(stock, fresh_only=True) is None


def test_cached_unfinished_daily_bar_is_removed_before_reuse(tmp_path, monkeypatch) -> None:
    from core import domestic_storage

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 4, 10, 30, tzinfo=KOREA)

    monkeypatch.setattr(domestic_storage, "datetime", FrozenDatetime)
    cache = DomesticCacheStore(tmp_path)
    stock = StockInfo("005930", "KOSPI", "삼성전자", "", "전기전자", False)
    cache.save_daily(stock, _frame_ending("2026-09-04"))
    loaded = cache.load_daily(stock, fresh_only=True)
    assert loaded is not None
    assert loaded.index[-1].date().isoformat() == "2026-09-03"


def test_missed_completed_weekday_forces_refresh_even_before_todays_close(tmp_path) -> None:
    path = tmp_path / "daily.csv"
    path.write_text("cache", encoding="utf-8")
    thursday = datetime(2026, 9, 3, 16, 0, tzinfo=KOREA).timestamp()
    os.utime(path, (thursday, thursday))
    frame = _frame_ending("2026-09-03")
    assert not _daily_cache_is_current(
        path, frame, now=datetime(2026, 9, 7, 9, 30, tzinfo=KOREA)
    )


def test_cached_current_minute_bucket_is_removed(tmp_path, monkeypatch) -> None:
    from core import domestic_storage

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 4, 10, 30, tzinfo=KOREA)

    monkeypatch.setattr(domestic_storage, "datetime", FrozenDatetime)
    cache = DomesticCacheStore(tmp_path)
    stock = StockInfo("005930", "KOSPI", "삼성전자", "", "전기전자", False)
    frame = _frame_ending("2026-09-04")
    frame.index = pd.DatetimeIndex(
        [f"{stamp:%Y-%m-%d} 09:00" for stamp in frame.index], tz=KOREA
    )
    frame.loc[pd.Timestamp("2026-09-04 10:00", tz=KOREA)] = frame.iloc[-1]
    cache.save_minute(stock, 60, frame)
    loaded = cache.load_minute(stock, 60, fresh_only=True)
    assert loaded is not None
    assert loaded.index[-1].hour == 9
