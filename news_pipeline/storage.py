from __future__ import annotations

"""Content-addressed raw evidence, processed batches, and local event index."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from .deduplication import cluster_events, deduplicate_articles, normalize_title
from .models import NormalizedEvent, RawArtifact
from .normalization import canonical_url, parse_datetime, stable_id


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_component(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in {"-", "_", "."} else "_" for character in str(value).strip())
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"안전하지 않은 경로 구성요소: {value!r}")
    return cleaned


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


class NewsDataStore:
    """Store immutable source responses and query normalized events locally."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.raw_root = self.root / "raw"
        self.processed_root = self.root / "processed" / "events"
        self.manifest_root = self.root / "manifests"
        self.forward_root = self.root / "forward"
        self.index_path = self.root / "index" / "events.sqlite3"

    def store_raw(
        self,
        source: str,
        payload: bytes,
        *,
        collected_at: str | None = None,
        suffix: str = "bin",
        content_type: str = "application/octet-stream",
        request_url: str = "",
        query: str = "",
    ) -> RawArtifact:
        timestamp = parse_datetime(collected_at or _utc_now())
        digest = sha256_bytes(payload)
        source_component = _safe_component(source.casefold())
        suffix_component = _safe_component(suffix.lstrip(".") or "bin")
        path = self.raw_root / source_component / timestamp.strftime("%Y") / timestamp.strftime("%m") / timestamp.strftime("%d") / f"{digest}.{suffix_component}"
        if path.exists():
            if sha256_bytes(path.read_bytes()) != digest:
                raise RuntimeError(f"content-addressed raw hash mismatch: {path}")
        else:
            _atomic_write(path, payload)
        return RawArtifact(
            source=source,
            data_hash=digest,
            path=str(path),
            collected_at=timestamp.isoformat(timespec="seconds"),
            content_type=content_type,
            request_url=request_url,
            query=query,
        )

    def cache_key(self, source: str, url: str, params: dict[str, Any] | None = None) -> str:
        canonical = json.dumps([source, url, params or {}], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256_bytes(canonical.encode("utf-8"))

    def cache_get(self, key: str, *, now: str | None = None) -> RawArtifact | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source, data_hash, path, collected_at, content_type, request_url, query, expires_at FROM response_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
        if row is None or parse_datetime(row[7]) <= parse_datetime(now or _utc_now()):
            return None
        path = Path(row[2])
        if not path.exists() or sha256_bytes(path.read_bytes()) != row[1]:
            return None
        return RawArtifact(
            source=row[0], data_hash=row[1], path=row[2], collected_at=row[3], content_type=row[4],
            request_url=row[5], query=row[6], from_cache=True,
        )

    def cache_put(self, key: str, artifact: RawArtifact, *, ttl: timedelta) -> None:
        expires_at = (parse_datetime(artifact.collected_at) + ttl).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO response_cache
                   (cache_key, source, data_hash, path, collected_at, content_type, request_url, query, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    key, artifact.source, artifact.data_hash, artifact.path, artifact.collected_at,
                    artifact.content_type, artifact.request_url, artifact.query, expires_at,
                ),
            )

    @staticmethod
    def read_artifact(artifact: RawArtifact) -> bytes:
        payload = Path(artifact.path).read_bytes()
        if sha256_bytes(payload) != artifact.data_hash:
            raise RuntimeError(f"raw artifact hash mismatch: {artifact.path}")
        return payload

    def ingest_events(self, events: Iterable[NormalizedEvent], *, source: str) -> tuple[list[NormalizedEvent], Path | None]:
        incoming = deduplicate_articles(events)
        if not incoming:
            return [], None
        existing = self.query_events()
        existing_ids = {item.event_id: item for item in existing}
        existing_articles = {
            (item.market, item.symbol, item.article_id): item for item in existing if item.article_id
        }
        existing_urls = {
            (item.market, item.symbol, canonical_url(item.source_url)): item for item in existing if item.source_url
        }
        existing_titles = {
            (item.market, item.symbol, normalize_title(item.title)): item
            for item in existing
            if normalize_title(item.title)
        }
        duplicates: dict[str, NormalizedEvent] = {}
        new_items: list[NormalizedEvent] = []
        for item in incoming:
            matched = existing_ids.get(item.event_id)
            if matched is None and item.article_id:
                matched = existing_articles.get((item.market, item.symbol, item.article_id))
            if matched is None and item.source_url:
                matched = existing_urls.get((item.market, item.symbol, canonical_url(item.source_url)))
            title_key = (item.market, item.symbol, normalize_title(item.title))
            if matched is None and title_key[2]:
                matched = existing_titles.get(title_key)
            if matched is None:
                new_items.append(item)
            else:
                # Keep the original first-seen/collection evidence. A cache
                # replay is not a new first observation of the article.
                duplicates[item.event_id] = matched

        new_ids = {item.event_id for item in new_items}
        combined = cluster_events([*existing, *new_items])
        clustered_by_id = {item.event_id: item for item in combined if item.event_id in new_ids}
        clustered = [duplicates.get(item.event_id) or clustered_by_id.get(item.event_id, item) for item in incoming]

        rows = [json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) for item in clustered]
        payload = ("\n".join(rows) + "\n").encode("utf-8")
        digest = sha256_bytes(payload)
        first_time = min(parse_datetime(item.event_time) for item in clustered)
        path = self.processed_root / _safe_component(source.casefold()) / first_time.strftime("%Y") / first_time.strftime("%m") / f"{digest}.jsonl"
        if not path.exists():
            _atomic_write(path, payload)

        with self._connect() as connection:
            for event in (item for item in clustered if item.event_id in new_ids):
                connection.execute(
                    """INSERT OR REPLACE INTO events
                       (event_id, market, symbol, event_type, event_time, first_seen_at, source_type,
                        article_id, cluster_id, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id, event.market, event.symbol, event.event_type, event.event_time,
                        event.first_seen_at, event.source_type, event.article_id, event.cluster_id,
                        json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True),
                    ),
                )
        return clustered, path

    def query_events(
        self,
        *,
        market: str | None = None,
        symbol: str | None = None,
        source_type: str | None = None,
    ) -> list[NormalizedEvent]:
        clauses: list[str] = []
        values: list[str] = []
        for column, value in (("market", market), ("symbol", symbol), ("source_type", source_type)):
            if value:
                clauses.append(f"{column}=?")
                values.append(value.upper() if column in {"market", "symbol"} else value)
        sql = "SELECT payload_json FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_time, event_id"
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [NormalizedEvent.from_dict(json.loads(row[0])) for row in rows]

    def write_manifest(self, collection_id: str, payload: dict[str, Any]) -> Path:
        body = dict(payload)
        body.setdefault("collection_id", collection_id)
        body.setdefault("created_at", _utc_now())
        path = self.manifest_root / f"{_safe_component(collection_id)}.json"
        _atomic_write(path, json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
        return path

    def archive_analysis(
        self,
        *,
        analysis_timestamp: str,
        market: str,
        symbol: str,
        query: str,
        source: str,
        event_ids: Iterable[str],
        raw_hashes: Iterable[str],
    ) -> Path:
        payload = {
            "schema_version": 2,
            "record_type": "SOURCE_CAPTURE",
            "analysis_id": stable_id(analysis_timestamp, market.upper(), symbol.upper(), prefix="analysis"),
            "analysis_timestamp": parse_datetime(analysis_timestamp).isoformat(timespec="seconds"),
            "market": market.upper(),
            "symbol": symbol.upper(),
            "query": query,
            "source": source,
            "event_news_ids": sorted(set(event_ids)),
            "raw_response_hashes": sorted(set(raw_hashes)),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = sha256_bytes(encoded)
        timestamp = parse_datetime(analysis_timestamp)
        path = self.forward_root / "analysis" / timestamp.strftime("%Y") / timestamp.strftime("%m") / timestamp.strftime("%d") / f"{digest}.json"
        if not path.exists():
            _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        return path

    def archive_analysis_snapshot(
        self,
        *,
        analysis_timestamp: str,
        market: str,
        symbol: str,
        company_name: str,
        technical_score: int,
        grade: str,
        technical_action: str,
        final_action: str,
        events: Iterable[NormalizedEvent],
        market_intelligence: dict[str, Any] | None = None,
        smoke_test: bool = False,
    ) -> Path:
        """Persist the complete evidence view after one analyzer result exists."""

        timestamp = parse_datetime(analysis_timestamp)
        analysis_id = stable_id(str(analysis_timestamp), market.upper(), symbol.upper(), prefix="analysis")
        visible = sorted(
            {
                item.event_id: item
                for item in events
                if item.first_seen_at
                and parse_datetime(item.event_time) <= timestamp
                and parse_datetime(item.first_seen_at) <= timestamp
            }.values(),
            key=lambda item: (item.event_time, item.event_id),
        )
        source_ids: dict[str, list[str]] = {
            "dart_event_ids": [],
            "edgar_event_ids": [],
            "naver_news_ids": [],
            "google_news_ids": [],
            "gdelt_candidate_ids": [],
        }
        source_keys = {
            "OPENDART": "dart_event_ids",
            "SEC_EDGAR": "edgar_event_ids",
            "NAVER_NEWS": "naver_news_ids",
            "GOOGLE_NEWS_RSS": "google_news_ids",
            "GDELT_GKG": "gdelt_candidate_ids",
            "GDELT_DOC": "gdelt_candidate_ids",
        }
        lineage: list[dict[str, Any]] = []
        raw_hashes: set[str] = set()
        for event in visible:
            key = source_keys.get(event.source_type)
            if key:
                source_ids[key].append(event.event_id)
            raw_value = event.raw_reference.get("raw_hash")
            event_hashes = sorted({str(value) for value in ([raw_value] if raw_value else []) if value})
            raw_hashes.update(event_hashes)
            lineage.append(
                {
                    "event_id": event.event_id,
                    "source_type": event.source_type,
                    "source_event_type": event.source_event_type,
                    "normalized_event_type": event.normalized_event_type,
                    "source_level": event.source_level,
                    "scope": event.scope,
                    "relevance_status": event.relevance_status,
                    "event_time_precision": event.event_time_precision,
                    "synthetic_time": event.synthetic_time,
                    "target_market_relevance": event.target_market_relevance,
                    "target_sector_relevance": event.target_sector_relevance,
                    "target_company_relevance": event.target_company_relevance,
                    "cluster_id": event.cluster_id,
                    "raw_hashes": event_hashes,
                }
            )
        payload: dict[str, Any] = {
            "schema_version": 2,
            "record_type": "ANALYSIS_SNAPSHOT",
            "analysis_id": analysis_id,
            "analysis_timestamp": timestamp.isoformat(timespec="seconds"),
            "market": market.upper(),
            "symbol": symbol.upper(),
            "company_name": company_name,
            "technical_score": int(technical_score),
            "grade": grade,
            "technical_action": technical_action,
            "final_action": final_action,
            "smoke_test": bool(smoke_test),
            **{key: sorted(set(value)) for key, value in source_ids.items()},
            "layers": {
                "raw_event_hashes": sorted(raw_hashes),
                "normalized_event_ids": [item.event_id for item in visible],
                "cluster_ids": sorted({item.cluster_id for item in visible if item.cluster_id}),
                "market_intelligence_result": market_intelligence,
            },
            "lineage": lineage,
            "forward_outcome_slots": {"1": None, "5": None, "10": None, "20": None},
        }
        path = self.forward_root / "analysis" / timestamp.strftime("%Y") / timestamp.strftime("%m") / timestamp.strftime("%d") / f"{analysis_id}.json"
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO analysis_snapshots (analysis_id, analysis_timestamp, market, symbol, path) VALUES (?, ?, ?, ?, ?)",
                (analysis_id, payload["analysis_timestamp"], market.upper(), symbol.upper(), str(path)),
            )
        return path

    def read_analysis_snapshot(self, analysis_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT path FROM analysis_snapshots WHERE analysis_id=?", (analysis_id,)).fetchone()
        if row is None:
            raise KeyError(f"analysis snapshot 없음: {analysis_id}")
        return json.loads(Path(row[0]).read_text(encoding="utf-8"))

    def archive_forward_outcomes(self, analysis_id: str, outcomes: dict[int, dict[str, Any]]) -> Path:
        """Store later 1/5/10/20-day outcomes separately from immutable inputs."""

        unexpected = set(outcomes) - {1, 5, 10, 20}
        if unexpected:
            raise ValueError(f"지원하지 않는 forward horizon: {sorted(unexpected)}")
        snapshot = self.read_analysis_snapshot(analysis_id)
        payload = {
            "schema_version": 1,
            "analysis_id": analysis_id,
            "analysis_timestamp": snapshot["analysis_timestamp"],
            "market": snapshot["market"],
            "symbol": snapshot["symbol"],
            "outcomes": {str(key): value for key, value in sorted(outcomes.items())},
            "recorded_at": _utc_now(),
        }
        path = self.forward_root / "outcomes" / f"{_safe_component(analysis_id)}.json"
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
        return path

    def _connect(self) -> sqlite3.Connection:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.index_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_time TEXT NOT NULL,
                first_seen_at TEXT,
                source_type TEXT NOT NULL,
                article_id TEXT,
                cluster_id TEXT,
                payload_json TEXT NOT NULL
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_symbol_time ON events(market, symbol, event_time)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_events_cluster ON events(cluster_id)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS response_cache (
                cache_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                data_hash TEXT NOT NULL,
                path TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                content_type TEXT,
                request_url TEXT,
                query TEXT,
                expires_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS analysis_snapshots (
                analysis_id TEXT PRIMARY KEY,
                analysis_timestamp TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                path TEXT NOT NULL
            )"""
        )
        return connection
