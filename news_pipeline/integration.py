from __future__ import annotations

"""Non-scoring bridge from completed analyzer results to the forward archive."""

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from .archive import archive_visible_items
from .point_in_time import events_as_of
from .storage import NewsDataStore


def archive_analyzed_result(
    store: NewsDataStore,
    result: Any,
    *,
    market: str,
    analysis_timestamp: str | None = None,
    smoke_test: bool | None = None,
) -> Path:
    """Archive the exact context attached to one completed individual analysis."""

    timestamp = analysis_timestamp or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    stock = result.stock
    intelligence = result.intelligence
    items = []
    if intelligence is not None:
        items.extend(intelligence.official_headlines)
        items.extend(intelligence.headlines)
        items.extend(intelligence.event_headlines)
    events = archive_visible_items(
        store,
        analysis_timestamp=timestamp,
        market=market,
        symbol=stock.symbol,
        company_name=getattr(stock, "english_name", "") or stock.display_name,
        query=f"analysis_snapshot:{market}:{stock.symbol}",
        source="analysis_snapshot",
        items=items,
    )
    visible = events_as_of(events, timestamp)
    intelligence_payload = intelligence.to_dict() if intelligence is not None else None
    is_smoke_test = (
        str(os.getenv("REAL_ANALYSIS_SMOKE_TEST", "")).strip().casefold()
        in {"1", "true", "yes", "on"}
        if smoke_test is None
        else bool(smoke_test)
    )
    return store.archive_analysis_snapshot(
        analysis_timestamp=timestamp,
        market=market,
        symbol=stock.symbol,
        company_name=getattr(stock, "english_name", "") or stock.display_name,
        technical_score=result.technical_score,
        grade=result.daily.grade,
        technical_action=result.daily.action,
        final_action=result.final_action,
        events=visible,
        market_intelligence=intelligence_payload,
        smoke_test=is_smoke_test,
    )


def try_archive_analyzed_result(
    store: NewsDataStore | None,
    result: Any,
    *,
    market: str,
    analysis_timestamp: str | None = None,
    smoke_test: bool | None = None,
) -> Path | None:
    """Keep an archive outage from turning a valid stock analysis into a failure."""

    if store is None:
        return None
    try:
        return archive_analyzed_result(
            store, result, market=market, analysis_timestamp=analysis_timestamp,
            smoke_test=smoke_test,
        )
    except Exception:
        return None
