from __future__ import annotations

"""Conservative article deduplication and event-cluster candidate creation."""

from dataclasses import replace
from datetime import timedelta
import re
from typing import Iterable

from .models import NormalizedEvent
from .normalization import canonical_url, parse_datetime, stable_id


_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at", "with", "from",
    "is", "are", "as", "by", "after", "before", "says", "said", "new", "update",
    "대한", "관련", "및", "으로", "에서", "한다", "했다", "공시", "속보",
}


def normalize_title(value: str) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\s+[-|–—]\s+[^-|–—]{2,40}$", "", text)
    text = re.sub(r"[^0-9a-z가-힣]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_tokens(value: str) -> frozenset[str]:
    return frozenset(token for token in normalize_title(value).split() if len(token) >= 2 and token not in _STOPWORDS)


def title_similarity(left: str, right: str) -> float:
    a, b = title_tokens(left), title_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def numeric_facts(value: str) -> frozenset[str]:
    """Keep material numbers/units so different contracts are not over-merged."""

    text = str(value or "").casefold().replace(",", "")
    pattern = r"(?:[$₩€£]\s*)?\d+(?:\.\d+)?\s*(?:조원|억원|만원|원|trillion|billion|million|bn|mn|%|percent|usd|krw)?"
    return frozenset(re.sub(r"\s+", "", item) for item in re.findall(pattern, text) if re.search(r"\d", item))


def _entity_tokens(values: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize_title(value) for value in values if normalize_title(value))


def deduplicate_articles(events: Iterable[NormalizedEvent]) -> list[NormalizedEvent]:
    output: list[NormalizedEvent] = []
    seen_ids: set[tuple[str, str, str]] = set()
    seen_urls: set[tuple[str, str, str]] = set()
    seen_title_keys: set[tuple[str, str, str]] = set()
    for event in sorted(events, key=lambda item: (item.event_time, item.source_type, item.event_id)):
        url = canonical_url(event.source_url)
        article_key = (event.market, event.symbol, event.article_id)
        url_key = (event.market, event.symbol, url)
        title_key = (event.market, event.symbol, normalize_title(event.title))
        if event.article_id and article_key in seen_ids:
            continue
        if url and url_key in seen_urls:
            continue
        if title_key[2] and title_key in seen_title_keys:
            continue
        output.append(event)
        if event.article_id:
            seen_ids.add(article_key)
        if url:
            seen_urls.add(url_key)
        if title_key[2]:
            seen_title_keys.add(title_key)
    return output


def cluster_events(
    events: Iterable[NormalizedEvent], *, similarity_threshold: float = 0.58, window_hours: int = 36
) -> list[NormalizedEvent]:
    """Assign deterministic *candidate* clusters; this is not semantic truth."""

    clustered: list[NormalizedEvent] = []
    representatives: list[NormalizedEvent] = []
    for event in deduplicate_articles(events):
        event_time = parse_datetime(event.event_time)
        explicit_key = str(event.raw_reference.get("gdelt_event_id") or event.raw_reference.get("accession_number") or event.raw_reference.get("receipt_number") or "")
        chosen: NormalizedEvent | None = None
        method = "singleton"
        for previous in representatives:
            if previous.market != event.market or previous.symbol != event.symbol or previous.event_type != event.event_type:
                continue
            if abs(event_time - parse_datetime(previous.event_time)) > timedelta(hours=window_hours):
                continue
            current_numbers = numeric_facts(event.title)
            previous_numbers = numeric_facts(previous.title)
            if current_numbers and previous_numbers and current_numbers.isdisjoint(previous_numbers):
                continue
            current_entities = _entity_tokens(event.entities)
            previous_entities = _entity_tokens(previous.entities)
            if current_entities and previous_entities and current_entities.isdisjoint(previous_entities):
                continue
            previous_key = str(previous.raw_reference.get("gdelt_event_id") or previous.raw_reference.get("accession_number") or previous.raw_reference.get("receipt_number") or "")
            if explicit_key and explicit_key == previous_key:
                chosen, method = previous, "source_event_id"
                break
            if event.title and previous.title:
                similarity = title_similarity(event.title, previous.title)
                if similarity >= similarity_threshold:
                    chosen, method = previous, "title_entity_time_candidate"
                    break
                if current_numbers and current_numbers == previous_numbers and similarity >= 0.35:
                    chosen, method = previous, "title_number_entity_time_candidate"
                    break
        if chosen is None:
            seed = explicit_key or normalize_title(event.title) or event.article_id or event.event_id
            cluster_id = stable_id(event.market, event.symbol, event.event_type, event_time.date(), seed, prefix="cluster")
            updated = replace(event, cluster_id=cluster_id, cluster_method=method)
            representatives.append(updated)
        else:
            updated = replace(event, cluster_id=chosen.cluster_id, cluster_method=method)
        clustered.append(updated)
    return clustered
