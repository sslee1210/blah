from __future__ import annotations

"""Serializable contracts for free news and official filing evidence."""

from dataclasses import asdict, dataclass, field
from typing import Any


EVENT_TYPES = {
    "EARNINGS",
    "GUIDANCE",
    "CAPITAL_RAISE",
    "BUYBACK",
    "DIVIDEND",
    "M&A",
    "SUPPLY_CONTRACT",
    "REGULATION",
    "PRODUCT",
    "TECHNOLOGY",
    "CYBER",
    "SANCTION",
    "TARIFF",
    "INTEREST_RATE",
    "FX",
    "COMMODITY",
    "GEOPOLITICS",
    "LAWSUIT",
    "MANAGEMENT_CHANGE",
    "CORPORATE_ACTION",
    "OTHER",
}

TRUST_LEVELS = {"TRUSTED", "PARTIALLY_TRUSTED", "EXPLORATORY", "NOT_EVALUABLE"}
SOURCE_LEVELS = {1, 2, 3}
EVENT_SCOPES = {"COMPANY", "SECTOR", "MARKET", "MACRO", "GLOBAL_EVENT"}
RELEVANCE_STATUSES = {
    "DIRECT_COMPANY",
    "INDIRECT_PRODUCT",
    "SECTOR_CANDIDATE",
    "GLOBAL_CANDIDATE",
    "REJECTED_GENERIC",
    "UNASSESSED",
}


@dataclass(frozen=True)
class CompanyTarget:
    market: str
    symbol: str
    company_name: str
    aliases: tuple[str, ...] = ()
    permanent_id: str = ""

    @property
    def all_names(self) -> tuple[str, ...]:
        values = (self.company_name, self.symbol, *self.aliases)
        return tuple(dict.fromkeys(item.strip() for item in values if item and item.strip()))


@dataclass(frozen=True)
class RawArtifact:
    source: str
    data_hash: str
    path: str
    collected_at: str
    content_type: str = "application/octet-stream"
    request_url: str = ""
    query: str = ""
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    market: str
    symbol: str
    company_name: str
    event_type: str
    event_time: str
    first_seen_at: str | None
    source_type: str
    source_name: str
    source_url: str
    title: str
    collected_at: str
    relevance: float | None = None
    direction: str | None = None
    confidence: float | None = None
    raw_reference: dict[str, Any] = field(default_factory=dict)
    query: str = ""
    article_id: str = ""
    cluster_id: str = ""
    cluster_method: str = ""
    event_time_precision: str = "second"
    pit_trust: str = "EXPLORATORY"
    is_correction: bool = False
    entities: tuple[str, ...] = ()
    source_event_type: str = ""
    normalized_event_type: str = ""
    source_level: int = 3
    scope: str = "COMPANY"
    relevance_status: str = "UNASSESSED"
    synthetic_time: bool = False
    target_market_relevance: str = "UNASSESSED"
    target_sector_relevance: str = "UNASSESSED"
    target_company_relevance: str = "UNASSESSED"

    def __post_init__(self) -> None:
        normalized = self.normalized_event_type or self.event_type
        if normalized not in EVENT_TYPES:
            raise ValueError(f"지원하지 않는 normalized_event_type: {normalized}")
        # event_type remains as a compatibility alias for existing consumers.
        object.__setattr__(self, "event_type", normalized)
        object.__setattr__(self, "normalized_event_type", normalized)
        if self.pit_trust not in TRUST_LEVELS:
            raise ValueError(f"지원하지 않는 pit_trust: {self.pit_trust}")
        if self.source_level not in SOURCE_LEVELS:
            raise ValueError(f"지원하지 않는 source_level: {self.source_level}")
        if self.scope not in EVENT_SCOPES:
            raise ValueError(f"지원하지 않는 scope: {self.scope}")
        if self.relevance_status not in RELEVANCE_STATUSES:
            raise ValueError(f"지원하지 않는 relevance_status: {self.relevance_status}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NormalizedEvent":
        payload = dict(value)
        payload["raw_reference"] = dict(payload.get("raw_reference") or {})
        payload["entities"] = tuple(payload.get("entities") or ())
        source_type = str(payload.get("source_type") or "").upper()
        if "source_level" not in payload:
            payload["source_level"] = (
                1 if source_type in {"OPENDART", "SEC_EDGAR"}
                else 2 if source_type in {"NAVER_NEWS", "GOOGLE_NEWS_RSS"}
                else 3
            )
        if "scope" not in payload:
            payload["scope"] = "MARKET" if str(payload.get("symbol") or "") == "*" else "COMPANY"
        if "normalized_event_type" not in payload:
            payload["normalized_event_type"] = str(payload.get("event_type") or "OTHER")
        if "source_event_type" not in payload:
            payload["source_event_type"] = str(
                payload["raw_reference"].get("filing_type")
                or payload["raw_reference"].get("report_type")
                or ""
            )
        return cls(**payload)


@dataclass
class CollectionResult:
    source: str
    collected_at: str
    events: list[NormalizedEvent] = field(default_factory=list)
    artifacts: list[RawArtifact] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.errors and not self.events:
            return "unavailable"
        if self.errors:
            return "partial"
        return "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "collected_at": self.collected_at,
            "status": self.status,
            "events": [item.to_dict() for item in self.events],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "errors": list(self.errors),
            "notes": list(self.notes),
            "metadata": dict(self.metadata),
        }
