from __future__ import annotations

"""Audit the local free-news archive without turning evidence into a score."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

from news_pipeline.deduplication import normalize_title
from news_pipeline.normalization import canonical_url, gdelt_company_relevance, parse_datetime
from news_pipeline.storage import NewsDataStore


PROJECT_ROOT = Path(__file__).resolve().parent


def audit(root: Path) -> dict[str, object]:
    store = NewsDataStore(root)
    events = store.query_events()
    by_source = Counter(item.source_type for item in events)
    by_market = Counter(item.market for item in events)
    by_symbol = Counter(f"{item.market}:{item.symbol}" for item in events)
    trust = Counter(item.pit_trust for item in events)
    source_levels = Counter(f"LEVEL_{item.source_level}" for item in events)
    scopes = Counter(item.scope for item in events)
    normalized_types = Counter(item.normalized_event_type for item in events)
    relevance = Counter(item.relevance_status for item in events)
    ranges: dict[str, dict[str, str]] = {}
    grouped_times: dict[str, list[datetime]] = defaultdict(list)
    invalid_order: list[str] = []
    unknown_first_seen: list[str] = []
    for item in events:
        grouped_times[item.source_type].append(parse_datetime(item.event_time))
        if not item.first_seen_at:
            unknown_first_seen.append(item.event_id)
        elif parse_datetime(item.first_seen_at) < parse_datetime(item.event_time):
            invalid_order.append(item.event_id)
    for source, values in grouped_times.items():
        ranges[source] = {"start": min(values).isoformat(), "end": max(values).isoformat()}

    article_keys = [(item.market, item.symbol, item.article_id) for item in events if item.article_id]
    url_keys = [(item.market, item.symbol, canonical_url(item.source_url)) for item in events if item.source_url]
    title_keys = [(item.market, item.symbol, normalize_title(item.title)) for item in events if normalize_title(item.title)]
    cluster_counts = Counter(item.cluster_id for item in events if item.cluster_id)
    gdelt_events = [item for item in events if item.source_type == "GDELT_GKG"]
    gdelt_kept: list[object] = []
    gdelt_rejected: list[object] = []
    for item in gdelt_events:
        assessment = gdelt_company_relevance(
            item.raw_reference.get("organizations", ()),
            company_name=item.company_name,
            symbol=item.symbol,
            aliases=item.raw_reference.get("matched_aliases", ()),
        )
        (gdelt_kept if assessment["accepted"] else gdelt_rejected).append(item)
    event_ids = {item.event_id for item in events}
    raw_hashes = {
        path.stem for path in (root / "raw").rglob("*") if path.is_file()
    }
    record_types: Counter[str] = Counter()
    snapshot_lineage_missing_events: list[str] = []
    snapshot_lineage_missing_raw: list[str] = []
    for path in (root / "forward" / "analysis").rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            record_types["MALFORMED"] += 1
            continue
        record_type = str(payload.get("record_type") or "LEGACY_SOURCE_CAPTURE")
        record_types[record_type] += 1
        if record_type != "ANALYSIS_SNAPSHOT":
            continue
        for row in payload.get("lineage", []):
            event_id = str(row.get("event_id") or "")
            if event_id and event_id not in event_ids:
                snapshot_lineage_missing_events.append(event_id)
            for raw_hash in row.get("raw_hashes", []):
                if raw_hash and raw_hash not in raw_hashes:
                    snapshot_lineage_missing_raw.append(str(raw_hash))

    source_assessment = {
        "GDELT_GKG": {
            "trust": "EXPLORATORY",
            "reason": "bulk batch time is reproducible, but organization extraction is high-recall and article title/full text are absent",
        },
        "OPENDART": {
            "trust": "PARTIALLY_TRUSTED",
            "reason": "official filings are historical, but list API provides receipt date rather than intraday time",
        },
        "SEC_EDGAR": {
            "trust": "PARTIALLY_TRUSTED",
            "reason": "official acceptance metadata is strong; ticker-history and filing-item edge cases still require validation",
        },
        "NAVER_NEWS": {
            "trust": "PARTIALLY_TRUSTED",
            "reason": "forward snapshots are PIT-safe after collected_at, but Search API is not a complete historical archive",
        },
        "GOOGLE_NEWS_RSS": {
            "trust": "EXPLORATORY",
            "reason": "current fallback snapshot only; endpoint is not a documented complete historical archive",
        },
    }
    return {
        "audited_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_count": len(events),
        "by_source": dict(sorted(by_source.items())),
        "by_market": dict(sorted(by_market.items())),
        "by_symbol": dict(sorted(by_symbol.items())),
        "pit_trust": dict(sorted(trust.items())),
        "source_levels": dict(sorted(source_levels.items())),
        "scopes": dict(sorted(scopes.items())),
        "normalized_event_types": dict(sorted(normalized_types.items())),
        "relevance_status": dict(sorted(relevance.items())),
        "coverage_ranges": ranges,
        "unknown_first_seen_count": len(unknown_first_seen),
        "first_seen_before_event_count": len(invalid_order),
        "duplicate_article_id_count": len(article_keys) - len(set(article_keys)),
        "duplicate_canonical_url_count": len(url_keys) - len(set(url_keys)),
        "duplicate_normalized_title_count": len(title_keys) - len(set(title_keys)),
        "multi_article_cluster_count": sum(count > 1 for count in cluster_counts.values()),
        "gdelt_relevance_replay": {
            "before_candidate_count": len(gdelt_events),
            "accepted_candidate_count": len(gdelt_kept),
            "rejected_candidate_count": len(gdelt_rejected),
            "accepted_by_symbol": dict(sorted(Counter(item.symbol for item in gdelt_kept).items())),
            "rejected_by_symbol": dict(sorted(Counter(item.symbol for item in gdelt_rejected).items())),
            "policy": "exact organization/legal alias; no ticker-only or substring promotion",
        },
        "forward_record_types": dict(sorted(record_types.items())),
        "snapshot_lineage_missing_event_count": len(snapshot_lineage_missing_events),
        "snapshot_lineage_missing_raw_count": len(snapshot_lineage_missing_raw),
        "raw_event_replay": not snapshot_lineage_missing_events and not snapshot_lineage_missing_raw,
        "source_assessment": source_assessment,
        "scoring_connected": False,
        "operational_snapshot_connected": record_types["ANALYSIS_SNAPSHOT"] > 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="무료 뉴스·공시 archive 품질 감사")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT / "data" / "news")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["first_seen_before_event_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
