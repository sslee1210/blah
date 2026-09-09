from __future__ import annotations

"""Small-window GDELT 2.0 Event, Mentions, and GKG bulk collector."""

from datetime import timedelta
from io import BytesIO, TextIOWrapper
import csv
import re
from typing import Any, Iterable
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

import requests

from ..models import CollectionResult, CompanyTarget, NormalizedEvent, RawArtifact
from ..normalization import (
    canonical_url,
    classify_event_type,
    gdelt_company_relevance,
    iso_utc,
    semicolon_values,
    stable_id,
)
from ..storage import NewsDataStore
from .common import CachedHttpClient, utc_now


GDELT_BASE_URL = "https://data.gdeltproject.org/gdeltv2"

_GKG_COLUMNS = {
    "record_id": 0,
    "published": 1,
    "source_collection": 2,
    "source_name": 3,
    "document_identifier": 4,
    "themes": 7,
    "enhanced_themes": 8,
    "locations": 9,
    "enhanced_locations": 10,
    "persons": 11,
    "enhanced_persons": 12,
    "organizations": 13,
    "enhanced_organizations": 14,
    "tone": 15,
    "gcam": 17,
}


class GdeltBulkClient(CachedHttpClient):
    source = "gdelt"

    def __init__(
        self,
        store: NewsDataStore,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(store, session=session, timeout=timeout)

    @staticmethod
    def dataset_url(timestamp: str, dataset: str) -> str:
        stamp = re.sub(r"\D", "", timestamp)
        if len(stamp) != 14 or int(stamp[-4:-2]) % 15:
            raise ValueError("GDELT timestamp는 15분 경계의 YYYYMMDDHHMMSS 형식이어야 합니다.")
        suffixes = {
            "gkg": "gkg.csv.zip",
            "events": "export.CSV.zip",
            "mentions": "mentions.CSV.zip",
        }
        if dataset not in suffixes:
            raise ValueError(f"지원하지 않는 GDELT dataset: {dataset}")
        return f"{GDELT_BASE_URL}/{stamp}.{suffixes[dataset]}"

    def fetch_batch(self, timestamp: str, dataset: str) -> tuple[bytes, RawArtifact]:
        url = self.dataset_url(timestamp, dataset)
        return self.get_bytes(
            self.source,
            url,
            suffix="zip",
            content_type="application/zip",
            query=f"{timestamp}:{dataset}",
            cache_ttl=timedelta(days=3650),
        )

    def collect_company_sample(
        self, *, timestamp: str, targets: Iterable[CompanyTarget], include_event_tables: bool = True
    ) -> CollectionResult:
        collected_at = utc_now()
        result = CollectionResult(source=self.source, collected_at=collected_at)
        target_list = list(targets)
        payloads: dict[str, bytes] = {}
        for dataset in (("gkg", "events", "mentions") if include_event_tables else ("gkg",)):
            try:
                payload, artifact = self.fetch_batch(timestamp, dataset)
                payloads[dataset] = payload
                result.artifacts.append(artifact)
            except Exception as exc:
                result.errors.append(f"GDELT {dataset} {timestamp} 실패: {str(exc)[:160]}")
        if "gkg" not in payloads:
            return result

        export_by_url: dict[str, list[dict[str, Any]]] = {}
        event_by_id: dict[str, dict[str, Any]] = {}
        for row in self.parse_events(payloads.get("events", b"")):
            event_by_id[row["gdelt_event_id"]] = row
            if row["source_url"]:
                export_by_url.setdefault(canonical_url(row["source_url"]), []).append(row)
        mentions_by_url: dict[str, list[dict[str, Any]]] = {}
        for row in self.parse_mentions(payloads.get("mentions", b"")):
            if row["mention_identifier"]:
                mentions_by_url.setdefault(canonical_url(row["mention_identifier"]), []).append(row)

        raw_hashes = {artifact.query.split(":")[-1]: artifact.data_hash for artifact in result.artifacts}
        result.events = self.parse_gkg(
            payloads["gkg"],
            targets=target_list,
            collected_at=collected_at,
            raw_hash=raw_hashes.get("gkg", ""),
            export_by_url=export_by_url,
            mentions_by_url=mentions_by_url,
        )
        result.events, processed = self.store.ingest_events(result.events, source=self.source)
        result.metadata.update(
            {
                "timestamp": re.sub(r"\D", "", timestamp),
                "processed_path": str(processed or ""),
                "matched_records": len(result.events),
                "event_rows": len(event_by_id),
                "mention_rows": sum(len(value) for value in mentions_by_url.values()),
                "title_available": False,
                "first_seen_basis": "GKG record batch timestamp",
            }
        )
        return result

    @classmethod
    def parse_gkg(
        cls,
        payload: bytes,
        *,
        targets: Iterable[CompanyTarget],
        collected_at: str,
        raw_hash: str,
        export_by_url: dict[str, list[dict[str, Any]]] | None = None,
        mentions_by_url: dict[str, list[dict[str, Any]]] | None = None,
    ) -> list[NormalizedEvent]:
        target_list = list(targets)
        output: list[NormalizedEvent] = []
        for fields in cls._zip_rows(payload):
            if len(fields) < 18:
                continue
            organizations = semicolon_values(fields[_GKG_COLUMNS["organizations"]])
            enhanced_organizations = semicolon_values(fields[_GKG_COLUMNS["enhanced_organizations"]], strip_offsets=True)
            org_values = tuple(dict.fromkeys((*organizations, *enhanced_organizations)))
            if not org_values:
                continue
            for target in target_list:
                relevance = gdelt_company_relevance(
                    org_values,
                    company_name=target.company_name,
                    symbol=target.symbol,
                    aliases=target.aliases,
                )
                if not relevance["accepted"]:
                    continue
                aliases = tuple(relevance["matches"])
                record_id = fields[_GKG_COLUMNS["record_id"]]
                published_raw = fields[_GKG_COLUMNS["published"]]
                try:
                    event_time = iso_utc(published_raw)
                    first_seen_at = iso_utc(record_id[:14])
                except (TypeError, ValueError):
                    continue
                source_url = fields[_GKG_COLUMNS["document_identifier"]]
                url_key = canonical_url(source_url)
                relations = list((export_by_url or {}).get(url_key, []))
                mentions = list((mentions_by_url or {}).get(url_key, []))
                relation_ids = tuple(dict.fromkeys(str(row.get("gdelt_event_id", "")) for row in (*relations, *mentions) if row.get("gdelt_event_id")))
                themes = semicolon_values(fields[_GKG_COLUMNS["themes"]])
                event_text = " ".join((*themes, source_url))
                event_type = classify_event_type(event_text)
                article_id = stable_id("GDELT_GKG", record_id, source_url, prefix="article")
                reference: dict[str, Any] = {
                    "raw_hash": raw_hash,
                    "gkg_record_id": record_id,
                    "published_at_raw": published_raw,
                    "source_collection": fields[_GKG_COLUMNS["source_collection"]],
                    "organizations": org_values,
                    "matched_aliases": aliases,
                    "persons": semicolon_values(fields[_GKG_COLUMNS["persons"]]),
                    "locations": semicolon_values(fields[_GKG_COLUMNS["locations"]]),
                    "themes": themes,
                    "tone": fields[_GKG_COLUMNS["tone"]],
                    "gcam": fields[_GKG_COLUMNS["gcam"]],
                    "gdelt_event_ids": relation_ids,
                    "mention_count_for_url": len(mentions),
                    "article_title_unavailable": True,
                    "relevance_gate": relevance["reason"],
                }
                if len(relation_ids) == 1:
                    reference["gdelt_event_id"] = relation_ids[0]
                output.append(
                    NormalizedEvent(
                        event_id=stable_id("GDELT_GKG", record_id, target.symbol, prefix="evt"),
                        market=target.market.upper(),
                        symbol=target.symbol.upper(),
                        company_name=target.company_name,
                        event_type=event_type,
                        event_time=event_time,
                        first_seen_at=first_seen_at,
                        source_type="GDELT_GKG",
                        source_name=fields[_GKG_COLUMNS["source_name"]] or urlsplit(source_url).netloc,
                        source_url=source_url,
                        title="",
                        collected_at=collected_at,
                        raw_reference=reference,
                        query=" | ".join(target.all_names),
                        article_id=article_id,
                        event_time_precision="second",
                        pit_trust="PARTIALLY_TRUSTED",
                        entities=org_values,
                        source_event_type=";".join(themes),
                        normalized_event_type=event_type,
                        source_level=3,
                        scope=str(relevance["scope"]),
                        relevance_status=str(relevance["status"]),
                        target_market_relevance="MEDIUM",
                        target_sector_relevance="UNASSESSED",
                        target_company_relevance="HIGH",
                    )
                )
        return output

    @classmethod
    def parse_events(cls, payload: bytes) -> list[dict[str, Any]]:
        if not payload:
            return []
        output: list[dict[str, Any]] = []
        for fields in cls._zip_rows(payload):
            if len(fields) < 61:
                continue
            output.append(
                {
                    "gdelt_event_id": fields[0],
                    "sql_date": fields[1],
                    "actor1_name": fields[6],
                    "actor2_name": fields[16],
                    "event_code": fields[27],
                    "event_base_code": fields[28],
                    "event_root_code": fields[29],
                    "quad_class": fields[30],
                    "goldstein_scale": fields[31],
                    "num_mentions": fields[32],
                    "num_sources": fields[33],
                    "num_articles": fields[34],
                    "average_tone": fields[35],
                    "date_added": fields[59],
                    "source_url": fields[60],
                }
            )
        return output

    @classmethod
    def parse_mentions(cls, payload: bytes) -> list[dict[str, Any]]:
        if not payload:
            return []
        output: list[dict[str, Any]] = []
        for fields in cls._zip_rows(payload):
            if len(fields) < 14:
                continue
            output.append(
                {
                    "gdelt_event_id": fields[0],
                    "event_time": fields[1],
                    "mention_time": fields[2],
                    "mention_type": fields[3],
                    "mention_source_name": fields[4],
                    "mention_identifier": fields[5],
                    "sentence_id": fields[6],
                    "confidence": fields[11],
                    "document_length": fields[12],
                    "document_tone": fields[13],
                }
            )
        return output

    @staticmethod
    def _zip_rows(payload: bytes) -> Iterable[list[str]]:
        try:
            archive = ZipFile(BytesIO(payload))
        except BadZipFile as exc:
            raise ValueError("GDELT 응답이 유효한 ZIP이 아닙니다.") from exc
        with archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if not names:
                return
            with archive.open(names[0]) as raw, TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="") as text:
                yield from csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
