from __future__ import annotations

"""Serializable contracts for historical price collection and auditing."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CollectionRequest:
    market: str
    symbol: str
    exchange: str
    kind: str = "stock"
    name: str = ""
    sector: str = ""
    security_type: str = "common_stock"
    listing_date: str | None = None
    delisting_date: str | None = None
    provider_symbol: str | None = None

    @property
    def key(self) -> str:
        return f"{self.market.upper()}:{self.kind}:{self.exchange}:{self.symbol}"


@dataclass(frozen=True)
class QualityIssue:
    severity: str
    code: str
    message: str
    count: int = 1
    examples: tuple[str, ...] = ()


@dataclass
class QualityReport:
    market: str
    symbol: str
    checked_at: str
    rows: int
    issues: list[QualityIssue] = field(default_factory=list)
    expected_sessions_known: bool = False

    @property
    def error_count(self) -> int:
        return sum(item.count for item in self.issues if item.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(item.count for item in self.issues if item.severity == "warning")

    @property
    def status(self) -> str:
        if self.error_count:
            return "failed"
        if self.warning_count:
            return "warning"
        return "passed"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.update(
            {
                "status": self.status,
                "error_count": self.error_count,
                "warning_count": self.warning_count,
            }
        )
        return value


@dataclass(frozen=True)
class AssetManifest:
    source: str
    market: str
    symbol: str
    security_type: str
    start_date: str | None
    end_date: str | None
    rows: int
    collected_at: str
    timezone: str
    price_adjustment: str
    corporate_action_policy: str
    survivorship_safe: bool
    point_in_time_level: str
    missing_days: int
    duplicate_rows: int
    data_hash: str
    exchange: str = ""
    kind: str = "stock"
    source_url: str = ""
    license_scope: str = ""
    listing_date: str | None = None
    delisting_date: str | None = None
    ticker_history_status: str = "unknown"
    raw_path: str = ""
    processed_path: str = ""
    quality_status: str = "unknown"
    exchange_calendar: str = ""
    calendar_version: str = ""
    early_close_sessions: int = 0
    notes: tuple[str, ...] = ()


@dataclass
class DatasetManifest:
    dataset_id: str
    dataset_version: int
    created_at: str
    updated_at: str
    analysis_commit: str | None
    baseline_modes: tuple[str, ...]
    status: str
    survivorship_safe: bool
    point_in_time_level: str
    purpose: str = "EVALUATION_DATASET"
    trust_level: str = "EXPLORATORY"
    delisted_securities_included: bool = False
    corporate_action_verified: bool = False
    exchange_calendar_verified: bool = False
    exchange_calendars: dict[str, str] = field(default_factory=dict)
    calendar_version: str = ""
    assets: list[AssetManifest] = field(default_factory=list)
    universe_snapshot_path: str | None = None
    intelligence_snapshot_path: str | None = None
    failures_path: str | None = None
    quality_report_path: str | None = None
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
