from __future__ import annotations

"""Re-audit an existing dataset without re-fetching or changing its prices."""

import argparse
from dataclasses import replace
import json
from pathlib import Path

import pandas as pd

from data_pipeline.calendars import CALENDAR_BY_MARKET, CALENDAR_VERSION, exchange_sessions
from data_pipeline.models import AssetManifest, DatasetManifest
from data_pipeline.quality import inspect_daily_prices
from data_pipeline.storage import HistoricalDataStore


ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="기존 평가 데이터셋 비파괴 재감사")
    parser.add_argument("--dataset", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = HistoricalDataStore(ROOT, args.dataset)
    if not store.manifest_path.exists():
        raise SystemExit(f"manifest가 없습니다: {store.manifest_path}")
    payload = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assets: list[AssetManifest] = []
    reports = []
    for item in payload.get("assets", []):
        asset = AssetManifest(**item)
        if not asset.processed_path:
            assets.append(asset)
            continue
        path = ROOT / asset.processed_path
        frame = _read_price(path)
        calendar = exchange_sessions(asset.market, frame.index.min(), frame.index.max())
        report = inspect_daily_prices(
            frame,
            market=asset.market,
            symbol=asset.symbol,
            timezone_name=asset.timezone,
            listing_date=asset.listing_date,
            delisting_date=asset.delisting_date,
            expected_sessions=calendar.sessions,
        )
        reports.append(report)
        assets.append(
            replace(
                asset,
                missing_days=sum(
                    issue.count for issue in report.issues
                    if issue.code == "missing_exchange_session"
                ),
                duplicate_rows=sum(
                    issue.count for issue in report.issues
                    if issue.code == "duplicate_session"
                ),
                quality_status=report.status,
                exchange_calendar=calendar.calendar_name,
                calendar_version=calendar.calendar_version,
                early_close_sessions=len(calendar.early_close_sessions),
            )
        )
    store.write_quality_reports(reports, preserve_existing=False)
    sources = {asset.source for asset in assets}
    sample = sources == {"kiwoom_rest_sample"}
    manifest = DatasetManifest(
        dataset_id=payload["dataset_id"],
        dataset_version=max(2, int(payload.get("dataset_version", 1))),
        created_at=payload["created_at"],
        updated_at=pd.Timestamp.now(tz="UTC").isoformat(),
        analysis_commit=payload.get("analysis_commit"),
        baseline_modes=tuple(payload.get("baseline_modes", ["TECHNICAL_BASELINE"])),
        status=payload.get("status", "collected"),
        survivorship_safe=bool(payload.get("survivorship_safe", False)),
        point_in_time_level=payload.get("point_in_time_level", "unknown"),
        purpose="PIPELINE_VALIDATION_ONLY" if sample else payload.get("purpose", "EVALUATION_DATASET"),
        trust_level="EXPLORATORY" if sample else payload.get("trust_level", "EXPLORATORY"),
        delisted_securities_included=bool(payload.get("delisted_securities_included", False)),
        corporate_action_verified=bool(payload.get("corporate_action_verified", False)),
        exchange_calendar_verified=True,
        exchange_calendars={
            market: CALENDAR_BY_MARKET[market]
            for market in sorted({asset.market for asset in assets})
        },
        calendar_version=CALENDAR_VERSION,
        assets=assets,
        universe_snapshot_path=payload.get("universe_snapshot_path"),
        intelligence_snapshot_path=payload.get("intelligence_snapshot_path"),
        failures_path=payload.get("failures_path"),
        quality_report_path=str(store.quality_json_path.relative_to(ROOT)),
        limitations=list(payload.get("limitations", [])),
    )
    store.write_manifest(manifest)
    print(f"dataset={manifest.dataset_id} purpose={manifest.purpose} trust={manifest.trust_level}")
    print(f"assets={len(assets)} calendar={manifest.exchange_calendars} version={CALENDAR_VERSION}")
    return 0


def _read_price(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    date_column = next(
        (name for name in ("timestamp", "date", "datetime") if name in frame),
        None,
    )
    if date_column is None:
        raise ValueError(f"날짜 열이 없습니다: {path}")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop(date_column), errors="raise"))
    return frame


if __name__ == "__main__":
    raise SystemExit(main())
