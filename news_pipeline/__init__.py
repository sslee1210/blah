"""Free news/disclosure collection and point-in-time event storage.

This package is deliberately separate from the analyzer's scoring code.  It
collects evidence; it does not decide whether the evidence is bullish or
bearish and it does not change any grade, action, or hard block.
"""

from .deduplication import cluster_events, deduplicate_articles, normalize_title
from .models import CompanyTarget, CollectionResult, NormalizedEvent, RawArtifact
from .point_in_time import events_as_of, point_in_time_status
from .storage import NewsDataStore, sha256_bytes

__all__ = [
    "CollectionResult",
    "CompanyTarget",
    "NewsDataStore",
    "NormalizedEvent",
    "RawArtifact",
    "cluster_events",
    "deduplicate_articles",
    "events_as_of",
    "normalize_title",
    "point_in_time_status",
    "sha256_bytes",
]
