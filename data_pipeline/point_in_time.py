from __future__ import annotations

"""Small, testable point-in-time gates used by dataset adapters."""

from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

import pandas as pd


PRICE_NAMES = ("open", "high", "low", "close")


@dataclass(frozen=True)
class SecurityHistory:
    permanent_id: str
    symbol: str
    exchange: str
    security_type: str
    listing_date: str | None = None
    delisting_date: str | None = None
    ticker_start_date: str | None = None
    ticker_end_date: str | None = None


def eligible_on(history: SecurityHistory, as_of: str | date | pd.Timestamp) -> bool:
    day = pd.Timestamp(as_of).date()
    if history.listing_date and day < pd.Timestamp(history.listing_date).date():
        return False
    if history.delisting_date and day > pd.Timestamp(history.delisting_date).date():
        return False
    if history.ticker_start_date and day < pd.Timestamp(history.ticker_start_date).date():
        return False
    if history.ticker_end_date and day > pd.Timestamp(history.ticker_end_date).date():
        return False
    return history.security_type.casefold() in {"common_stock", "common", "cs", "stock"}


def point_in_time_slice(
    frame: pd.DataFrame,
    *,
    as_of: str | date | pd.Timestamp,
    timezone: str,
    history: SecurityHistory | None = None,
) -> pd.DataFrame:
    """Return only observations knowable by the requested market date.

    Daily bars are keyed by exchange session date.  Intraday timestamps must
    be timezone-aware; they are converted to the market timezone before the
    cutoff is applied.  The function never forward-fills missing sessions.
    """

    if history is not None and not eligible_on(history, as_of):
        return frame.iloc[0:0].copy()
    result = frame.copy()
    index = pd.DatetimeIndex(result.index)
    cutoff_day = pd.Timestamp(as_of).date()
    if index.tz is None:
        mask = index.date <= cutoff_day
    else:
        market_zone = ZoneInfo(timezone)
        mask = index.tz_convert(market_zone).date <= cutoff_day
    return result.loc[mask].copy()


def price_views(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """Separate as-traded signal prices from corporate-action-safe outcomes.

    A provider may publish both ``raw_*`` and ``adjusted_*`` columns.  Raw
    prices feed historical signals; adjusted prices feed only future outcome
    calculations.  If the split is unavailable, both views use the standard
    columns and the returned policy is explicitly unverified.
    """

    raw_columns = {name: f"raw_{name}" for name in PRICE_NAMES}
    adjusted_columns = {name: f"adjusted_{name}" for name in PRICE_NAMES}
    has_raw = all(column in frame.columns for column in raw_columns.values())
    has_adjusted = all(column in frame.columns for column in adjusted_columns.values())
    signal = frame.copy()
    outcome = frame.copy()
    if has_raw:
        for target, source in raw_columns.items():
            signal[target] = frame[source]
        if "raw_volume" in frame:
            signal["volume"] = frame["raw_volume"]
        if "raw_trade_value" in frame:
            signal["trade_value"] = frame["raw_trade_value"]
    if has_adjusted:
        for target, source in adjusted_columns.items():
            outcome[target] = frame[source]
        if "adjusted_volume" in frame:
            outcome["volume"] = frame["adjusted_volume"]
    if has_raw and has_adjusted:
        return signal, outcome, "raw_for_signal_adjusted_for_outcome"
    return signal, outcome, "single_price_series_unverified"
