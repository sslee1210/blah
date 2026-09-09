from __future__ import annotations

"""Resumable collector orchestration with explicit partial-failure records."""

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Protocol

import pandas as pd

from .calendars import CALENDAR_BY_MARKET, CALENDAR_VERSION, exchange_sessions
from .models import AssetManifest, CollectionRequest, DatasetManifest, QualityReport
from .quality import inspect_daily_prices
from .storage import HistoricalDataStore, current_git_commit


class DailyPriceProvider(Protocol):
    source_name: str
    source_url: str
    license_scope: str
    timezone_by_market: dict[str, str]
    price_adjustment: str
    corporate_action_policy: str
    survivorship_safe: bool
    point_in_time_level: str
    limitations: tuple[str, ...]

    def fetch_daily(self, request: CollectionRequest) -> pd.DataFrame: ...


class ResumableCollector:
    def __init__(
        self,
        store: HistoricalDataStore,
        provider: DailyPriceProvider,
        *,
        max_attempts: int = 3,
        retry_seconds: float = 1.0,
        request_interval_seconds: float = 0.0,
    ) -> None:
        self.store = store
        self.provider = provider
        self.max_attempts = max(1, max_attempts)
        self.retry_seconds = max(0.0, retry_seconds)
        self.request_interval_seconds = max(0.0, request_interval_seconds)

    def collect(
        self,
        requests: list[CollectionRequest],
        *,
        resume: bool = True,
        universe_snapshot_path: str | None = None,
    ) -> DatasetManifest:
        checkpoint = self.store.load_checkpoint() if resume else {
            "dataset_id": self.store.dataset_id,
            "completed": {},
            "failed": {},
        }
        checkpoint.setdefault("completed", {})
        checkpoint.setdefault("failed", {})
        assets: list[AssetManifest] = []
        reports: list[QualityReport] = []
        total = len(requests)
        for number, request in enumerate(requests, 1):
            completed = checkpoint["completed"].get(request.key)
            if completed and self._completed_artifact_is_intact(completed):
                assets.append(AssetManifest(**completed["manifest"]))
                print(f"[{number}/{total}] {request.key} 건너뜀 (hash 확인 완료)")
                continue
            print(f"[{number}/{total}] {request.key} 수집")
            frame: pd.DataFrame | None = None
            last_error: Exception | None = None
            for attempt in range(1, self.max_attempts + 1):
                try:
                    frame = self.provider.fetch_daily(request)
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < self.max_attempts and self.retry_seconds:
                        time.sleep(self.retry_seconds * attempt)
            if frame is None:
                failure = {
                    "request": asdict(request),
                    "failed_at": _utc_now(),
                    "attempts": self.max_attempts,
                    "error_type": type(last_error).__name__ if last_error else "UnknownError",
                    "error": str(last_error or "unknown error"),
                }
                checkpoint["failed"][request.key] = failure
                self.store.append_failure(failure)
                self.store.save_checkpoint(checkpoint)
                print(f"  실패: {failure['error']}")
                continue
            timezone_name = self.provider.timezone_by_market[request.market.upper()]
            calendar = exchange_sessions(request.market, frame.index.min(), frame.index.max())
            report = inspect_daily_prices(
                frame,
                market=request.market,
                symbol=request.symbol,
                timezone_name=timezone_name,
                listing_date=request.listing_date,
                delisting_date=request.delisting_date,
                expected_sessions=calendar.sessions,
            )
            reports.append(report)
            raw_path, processed_path, data_hash = self.store.write_price_artifact(
                frame,
                source=self.provider.source_name,
                market=request.market,
                symbol=request.symbol,
                kind=request.kind,
                publish_processed=report.error_count == 0,
            )
            missing_days = sum(
                item.count for item in report.issues if item.code == "missing_exchange_session"
            )
            duplicate_rows = sum(
                item.count for item in report.issues if item.code == "duplicate_session"
            )
            manifest = AssetManifest(
                source=self.provider.source_name,
                source_url=self.provider.source_url,
                license_scope=self.provider.license_scope,
                market=request.market.upper(),
                symbol=request.symbol,
                exchange=request.exchange,
                kind=request.kind,
                security_type=request.security_type,
                start_date=_date_bound(frame, "min"),
                end_date=_date_bound(frame, "max"),
                rows=len(frame),
                collected_at=_utc_now(),
                timezone=timezone_name,
                price_adjustment=self.provider.price_adjustment,
                corporate_action_policy=self.provider.corporate_action_policy,
                survivorship_safe=self.provider.survivorship_safe,
                point_in_time_level=self.provider.point_in_time_level,
                missing_days=missing_days,
                duplicate_rows=duplicate_rows,
                data_hash=data_hash,
                listing_date=request.listing_date,
                delisting_date=request.delisting_date,
                ticker_history_status=("provided" if request.provider_symbol and request.provider_symbol != request.symbol else "not_available"),
                raw_path=str(raw_path.relative_to(self.store.project_root)),
                processed_path=(
                    str(processed_path.relative_to(self.store.project_root))
                    if report.error_count == 0
                    else ""
                ),
                quality_status=report.status,
                exchange_calendar=calendar.calendar_name,
                calendar_version=calendar.calendar_version,
                early_close_sessions=len(calendar.early_close_sessions),
                notes=(
                    "기존 Kiwoom client가 정렬·중복 제거 후 반환한 provider-normalized 표본입니다. wire payload 원본은 아닙니다.",
                ) if self.provider.source_name == "kiwoom_rest_sample" else (),
            )
            assets.append(manifest)
            checkpoint["completed"][request.key] = {
                "manifest": asdict(manifest),
                "data_hash": data_hash,
                "raw_path": manifest.raw_path,
            }
            checkpoint["failed"].pop(request.key, None)
            self.store.save_checkpoint(checkpoint)
            if self.request_interval_seconds and number < total:
                time.sleep(self.request_interval_seconds)

        quality_json, _ = self.store.write_quality_reports(reports)
        stock_assets = [item for item in assets if item.kind == "stock"]
        safe = (
            bool(stock_assets)
            and bool(universe_snapshot_path)
            and all(item.survivorship_safe for item in stock_assets)
        )
        now = _utc_now()
        manifest = DatasetManifest(
            dataset_id=self.store.dataset_id,
            dataset_version=1,
            created_at=_manifest_created_at(self.store.manifest_path, now),
            updated_at=now,
            analysis_commit=current_git_commit(self.store.project_root),
            baseline_modes=("TECHNICAL_BASELINE",),
            status="partial" if checkpoint["failed"] else "collected",
            survivorship_safe=safe,
            point_in_time_level=(
                self.provider.point_in_time_level if safe else "current-universe sample; exploratory only"
            ),
            purpose=(
                "PIPELINE_VALIDATION_ONLY"
                if self.provider.source_name == "kiwoom_rest_sample"
                else "EVALUATION_DATASET"
            ),
            # Asset collection alone cannot prove delisting-return and
            # corporate-action completeness.  A later dataset audit must
            # explicitly promote this state.
            trust_level="EXPLORATORY",
            delisted_securities_included=False,
            corporate_action_verified=False,
            exchange_calendar_verified=True,
            exchange_calendars=CALENDAR_BY_MARKET,
            calendar_version=CALENDAR_VERSION,
            assets=assets,
            universe_snapshot_path=universe_snapshot_path,
            failures_path=(
                str(self.store.failure_path.relative_to(self.store.project_root))
                if self.store.failure_path.exists()
                else None
            ),
            quality_report_path=str(quality_json.relative_to(self.store.project_root)),
            limitations=(
                list(getattr(self.provider, "limitations", ()))
                + ([] if universe_snapshot_path else ["날짜별 적격 universe snapshot이 없습니다."])
            ) if not safe else [],
        )
        self.store.write_manifest(manifest)
        return manifest

    def _completed_artifact_is_intact(self, completed: dict[str, object]) -> bool:
        raw_value = completed.get("raw_path")
        expected = completed.get("data_hash")
        if not raw_value or not expected:
            return False
        path = self.store.project_root / str(raw_value)
        if not path.exists():
            return False
        from .storage import sha256_file

        return sha256_file(path) == expected


def _date_bound(frame: pd.DataFrame, operation: str) -> str | None:
    if frame.empty:
        return None
    value = frame.index.min() if operation == "min" else frame.index.max()
    return pd.Timestamp(value).date().isoformat()


def _manifest_created_at(path: Path, fallback: str) -> str:
    if not path.exists():
        return fallback
    try:
        import json

        return str(json.loads(path.read_text(encoding="utf-8")).get("created_at") or fallback)
    except (OSError, ValueError, TypeError):
        return fallback


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
