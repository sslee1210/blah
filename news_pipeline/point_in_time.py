from __future__ import annotations

"""Point-in-time visibility rules for news, filings, and events."""

from datetime import datetime
from typing import Iterable

from .models import NormalizedEvent
from .normalization import parse_datetime


def point_in_time_status(event: NormalizedEvent, as_of: str | datetime) -> tuple[bool, str]:
    cutoff = parse_datetime(as_of)
    if parse_datetime(event.event_time) > cutoff:
        return False, "event_after_as_of"
    if not event.first_seen_at:
        return False, "first_seen_unknown"
    if parse_datetime(event.first_seen_at) > cutoff:
        return False, "first_seen_after_as_of"
    return True, "visible"


def events_as_of(
    events: Iterable[NormalizedEvent],
    as_of: str | datetime,
    *,
    market: str | None = None,
    symbol: str | None = None,
    require_first_seen: bool = True,
) -> list[NormalizedEvent]:
    cutoff = parse_datetime(as_of)
    output: list[NormalizedEvent] = []
    for event in events:
        if market and event.market.upper() != market.upper():
            continue
        if symbol and event.symbol.upper() != symbol.upper():
            continue
        if parse_datetime(event.event_time) > cutoff:
            continue
        if event.first_seen_at:
            if parse_datetime(event.first_seen_at) > cutoff:
                continue
        elif require_first_seen:
            continue
        output.append(event)
    return sorted(output, key=lambda item: (item.event_time, item.event_id))
