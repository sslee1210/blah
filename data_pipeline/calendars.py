from __future__ import annotations

"""Versioned exchange-session calendars for data quality and PIT cutoffs."""

from dataclasses import dataclass
import json
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd


CALENDAR_BY_MARKET = {"US": "XNYS", "KR": "XKRX"}
_OVERRIDE_PATH = Path(__file__).with_name("calendar_overrides.json")
_OVERRIDES = json.loads(_OVERRIDE_PATH.read_text(encoding="utf-8"))
CALENDAR_VERSION = f"exchange-calendars-{xcals.__version__}+{_OVERRIDES['override_version']}"


@dataclass(frozen=True)
class SessionCalendarResult:
    market: str
    calendar_name: str
    calendar_version: str
    sessions: pd.DatetimeIndex
    early_close_sessions: pd.DatetimeIndex


def exchange_sessions(
    market: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> SessionCalendarResult:
    """Return regular sessions and early closes without inventing weekdays."""

    key = market.upper()
    if key not in CALENDAR_BY_MARKET:
        raise ValueError("market은 US 또는 KR이어야 합니다.")
    name = CALENDAR_BY_MARKET[key]
    calendar = xcals.get_calendar(name)
    start_day = pd.Timestamp(start).tz_localize(None).normalize()
    end_day = pd.Timestamp(end).tz_localize(None).normalize()
    schedule = calendar.schedule.loc[start_day:end_day]
    closed_overrides = {
        pd.Timestamp(item["date"]).normalize()
        for item in _OVERRIDES.get("closed_sessions", {}).get(name, [])
    }
    if closed_overrides and not schedule.empty:
        normalized_index = pd.DatetimeIndex(schedule.index).tz_localize(None).normalize()
        schedule = schedule.loc[~normalized_index.isin(closed_overrides)]
    sessions = pd.DatetimeIndex(schedule.index).tz_localize(None).normalize()
    if schedule.empty:
        early = sessions[:0]
    else:
        durations = schedule["close"] - schedule["open"]
        reference = calendar.schedule.loc[
            start_day - pd.Timedelta(days=40): end_day + pd.Timedelta(days=40)
        ]
        regular_duration = (reference["close"] - reference["open"]).mode().iloc[0]
        early = pd.DatetimeIndex(schedule.index[durations < regular_duration]).tz_localize(None).normalize()
        explicit_early = pd.DatetimeIndex(
            [
                pd.Timestamp(item["date"])
                for item in _OVERRIDES.get("early_close_sessions", {}).get(name, [])
            ]
        ).normalize()
        early = early.union(explicit_early.intersection(sessions))
    return SessionCalendarResult(key, name, CALENDAR_VERSION, sessions, early)


def session_status(market: str, day: str | pd.Timestamp) -> str:
    """Classify a date as CLOSED, REGULAR or EARLY_CLOSE."""

    value = pd.Timestamp(day).tz_localize(None).normalize()
    result = exchange_sessions(market, value, value)
    if value not in result.sessions:
        return "CLOSED"
    if value in result.early_close_sessions:
        return "EARLY_CLOSE"
    return "REGULAR"
