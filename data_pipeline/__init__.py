"""Historical-data foundation for point-in-time analyzer evaluation.

This package is deliberately separate from the production analyzers.  It may
collect, validate and adapt data, but it does not change grades, scores, hard
blocks or final actions.
"""

from .models import (
    AssetManifest,
    CollectionRequest,
    DatasetManifest,
    QualityIssue,
    QualityReport,
)
from .calendars import CALENDAR_VERSION, exchange_sessions, session_status
from .point_in_time import SecurityHistory, eligible_on, point_in_time_slice, price_views
from .quality import inspect_daily_prices
from .storage import HistoricalDataStore, sha256_bytes, sha256_file
from .krx_open_api import (
    KrxHistoricalAdapter,
    KrxOpenApiClient,
    audit_historical_membership,
    krx_auth_key_from_environment,
    publish_krx_batch,
)

__all__ = [
    "AssetManifest",
    "CALENDAR_VERSION",
    "CollectionRequest",
    "DatasetManifest",
    "HistoricalDataStore",
    "KrxHistoricalAdapter",
    "KrxOpenApiClient",
    "QualityIssue",
    "QualityReport",
    "SecurityHistory",
    "eligible_on",
    "audit_historical_membership",
    "exchange_sessions",
    "inspect_daily_prices",
    "krx_auth_key_from_environment",
    "point_in_time_slice",
    "price_views",
    "publish_krx_batch",
    "sha256_bytes",
    "sha256_file",
    "session_status",
]
