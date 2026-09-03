from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.kiwoom_rest import StockInfo, latest_completed_us_weekday
from core.storage import CacheStore


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
