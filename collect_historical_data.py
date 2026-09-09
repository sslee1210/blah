from __future__ import annotations

"""Collect historical-data inputs without changing analyzer rules.

The built-in Kiwoom source deliberately labels its output as exploratory: it
does not provide historical point-in-time universes or complete delistings.
The KRX source is credential-gated and preserves date cross-sections, but it
also remains exploratory until delistings and corporate actions are audited.
"""

import argparse
from pathlib import Path

import pandas as pd

from data_pipeline.calendars import exchange_sessions
from data_pipeline.collector import ResumableCollector
from data_pipeline.kiwoom_sample import KiwoomSampleProvider, credentials_from_project, sample_requests
from data_pipeline.krx_open_api import (
    KrxHistoricalAdapter,
    KrxOpenApiClient,
    audit_historical_membership,
    estimated_request_count,
    krx_auth_key_from_environment,
    publish_krx_batch,
)
from data_pipeline.storage import HistoricalDataStore


ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PIT 평가용 과거 데이터 수집 기반")
    parser.add_argument(
        "--source", choices=("kiwoom-sample", "krx-open-api"), default="kiwoom-sample"
    )
    parser.add_argument("--market", choices=("US", "KR", "BOTH"), default="BOTH")
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--dataset", default="historical_sample")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.source == "krx-open-api":
        return _collect_krx(args)
    if not 1 <= args.sample_size <= 30:
        raise SystemExit("--sample-size는 소규모 E2E 검증을 위해 1~30이어야 합니다.")
    credentials = credentials_from_project(ROOT)
    provider = KiwoomSampleProvider(credentials)
    requests, universe_rows = sample_requests(
        provider,
        project_root=ROOT,
        market=args.market,
        sample_size=args.sample_size,
    )
    store = HistoricalDataStore(ROOT, args.dataset)
    universe_path = store.write_universe_snapshots(universe_rows)
    collector = ResumableCollector(
        store,
        provider,
        max_attempts=args.max_attempts,
        retry_seconds=1.0,
    )
    manifest = collector.collect(
        requests,
        resume=not args.no_resume,
        universe_snapshot_path=str(universe_path.relative_to(ROOT)),
    )
    stock_rows: dict[str, list[dict[str, object]]] = {"US": [], "KR": []}
    request_by_key = {(item.market, item.symbol): item for item in requests}
    for asset in manifest.assets:
        if asset.kind != "stock" or not asset.processed_path:
            continue
        request = request_by_key[(asset.market, asset.symbol)]
        stock_rows[asset.market].append(
            {
                "symbol": asset.symbol,
                "exchange": asset.exchange,
                "name": request.name,
                "korean_name": request.name,
                "english_name": request.name,
                "sector": request.sector,
                "security_type": asset.security_type,
                "is_etf": False,
                "listing_date": asset.listing_date or "",
                "delisting_date": asset.delisting_date or "",
                "permanent_id": "",
            }
        )
    for market, rows in stock_rows.items():
        if rows:
            store.write_stock_metadata(market, rows)
    print(f"dataset: {store.dataset_root}")
    print(f"manifest: {store.manifest_path}")
    print("주의: 현재 universe 표본이므로 신뢰 가능한 PIT 성능 측정에는 사용할 수 없습니다.")
    return 0


def _collect_krx(args: argparse.Namespace) -> int:
    key = krx_auth_key_from_environment()
    print("KRX_AUTH_KEY=" + ("AVAILABLE" if key else "MISSING"))
    if not key:
        print("KRX OPEN API 인증키가 없어 실제 historical universe 수집을 시작하지 않았습니다.")
        return 2
    if args.market == "US":
        raise SystemExit("KRX OPEN API source는 --market KR 또는 BOTH에서만 사용할 수 있습니다.")
    if not args.start_date or not args.end_date:
        raise SystemExit("KRX OPEN API source에는 --start-date와 --end-date가 필요합니다.")
    sessions = exchange_sessions("KR", args.start_date, args.end_date).sessions
    request_count = estimated_request_count(len(sessions))
    if request_count > 9_500:
        raise SystemExit(
            f"예상 {request_count:,}회 호출은 KRX 일 10,000회 한도에 너무 가깝습니다. 기간을 나누십시오."
        )
    dates = [pd.Timestamp(value).date().isoformat() for value in sessions]
    client = KrxOpenApiClient(key)
    batch = KrxHistoricalAdapter(client).collect_dates(dates)
    store = HistoricalDataStore(ROOT, args.dataset)
    manifest = publish_krx_batch(store, batch, resume=not args.no_resume)
    membership = audit_historical_membership(batch.universe_rows)
    print(f"dataset: {store.dataset_root}")
    print(f"manifest: {store.manifest_path}")
    print(f"sessions={len(dates)} requests={request_count} universe_dates={membership['snapshot_count']}")
    print(f"historical_only_symbols={len(membership['historical_only_symbols'])}")
    print(f"trust={manifest.trust_level} corporate_action_verified={manifest.corporate_action_verified}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
