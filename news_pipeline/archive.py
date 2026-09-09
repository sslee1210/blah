from __future__ import annotations

"""Adapter used by the existing analyzer to archive exactly the evidence it saw."""

from typing import Any, Iterable

from .models import NormalizedEvent
from .normalization import canonical_url, classify_event_type, classify_scope, iso_utc, source_level, stable_id
from .point_in_time import events_as_of
from .storage import NewsDataStore


def archive_visible_items(
    store: NewsDataStore,
    *,
    analysis_timestamp: str,
    market: str,
    symbol: str,
    company_name: str,
    query: str,
    source: str,
    items: Iterable[Any],
) -> list[NormalizedEvent]:
    """Archive current results without feeding anything back into scoring."""

    events: list[NormalizedEvent] = []
    raw_hashes: list[str] = []
    for item in items:
        title = str(getattr(item, "title", "") or "")
        source_url = str(getattr(item, "url", "") or "")
        published_at = str(getattr(item, "published_at", "") or "")
        collected_at = str(getattr(item, "collected_at", "") or analysis_timestamp)
        source_type = str(getattr(item, "source_type", "") or source).upper().replace(" ", "_")
        normalized_type = str(getattr(item, "normalized_event_type", "") or classify_event_type(title))
        source_scope = str(getattr(item, "scope", "") or classify_scope(title, default="MARKET" if symbol == "*" else "COMPANY"))
        raw_hash = str(getattr(item, "raw_hash", "") or "")
        if raw_hash:
            raw_hashes.append(raw_hash)
        if not title or not published_at:
            continue
        try:
            event_time = iso_utc(published_at)
            first_seen_at = iso_utc(collected_at)
        except (TypeError, ValueError):
            continue
        article_id = stable_id(canonical_url(source_url), title, prefix="article")
        events.append(
            NormalizedEvent(
                event_id=stable_id(source_type, article_id, market, symbol, prefix="evt"),
                market=market.upper(),
                symbol=symbol.upper(),
                company_name=company_name,
                event_type=normalized_type,
                event_time=event_time,
                first_seen_at=first_seen_at,
                source_type=source_type,
                source_name=str(getattr(item, "source", "") or source),
                source_url=source_url,
                title=title,
                collected_at=first_seen_at,
                raw_reference={
                    "raw_hash": raw_hash,
                    "archive_origin": "market_intelligence",
                    "original_link": str(getattr(item, "original_link", "") or source_url),
                    "provider_link": str(getattr(item, "provider_link", "") or ""),
                    "published_at_raw": published_at,
                },
                query=query,
                article_id=article_id,
                event_time_precision=str(getattr(item, "event_time_precision", "second") or "second"),
                pit_trust="TRUSTED" if source_type == "NAVER_NEWS" else "PARTIALLY_TRUSTED",
                entities=tuple(str(value) for value in (getattr(item, "entities", ()) or ()) if value),
                source_event_type=str(getattr(item, "source_event_type", "") or "NEWS_ARTICLE"),
                normalized_event_type=normalized_type,
                source_level=int(getattr(item, "source_level", 0) or source_level(source_type)),
                scope=source_scope,
                relevance_status=str(getattr(item, "relevance_status", "") or "UNASSESSED"),
                synthetic_time=bool(getattr(item, "synthetic_time", False)),
                target_market_relevance=str(
                    getattr(item, "target_market_relevance", "") or "UNASSESSED"
                ),
                target_sector_relevance=str(
                    getattr(item, "target_sector_relevance", "") or "UNASSESSED"
                ),
                target_company_relevance=str(
                    getattr(item, "target_company_relevance", "") or "UNASSESSED"
                ),
            )
        )
    events, _ = store.ingest_events(events, source=source.casefold())
    visible = events_as_of(events, analysis_timestamp)
    store.archive_analysis(
        analysis_timestamp=analysis_timestamp,
        market=market,
        symbol=symbol,
        query=query,
        source=source,
        event_ids=(item.event_id for item in visible),
        raw_hashes=raw_hashes,
    )
    return visible
