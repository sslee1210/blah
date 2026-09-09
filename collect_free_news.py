from __future__ import annotations

"""Collect free/public news and official filings without changing analyzer scores."""

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys

from news_pipeline.models import CollectionResult, CompanyTarget
from news_pipeline.sources import GdeltBulkClient, GoogleNewsRssClient, NaverNewsClient, OpenDartClient, SecEdgarClient
from news_pipeline.storage import NewsDataStore


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_NEWS_ROOT = PROJECT_ROOT / "data" / "news"

SAMPLE_TARGETS = (
    CompanyTarget("KR", "005930", "삼성전자", ("Samsung Electronics", "삼성전자 주식회사")),
    CompanyTarget("KR", "000660", "SK하이닉스", ("SK Hynix", "SK hynix")),
    CompanyTarget("KR", "005380", "현대차", ("Hyundai Motor", "Hyundai Motor Company", "현대자동차")),
    CompanyTarget("US", "AAPL", "Apple", ("Apple Inc", "Apple Inc.")),
    CompanyTarget("US", "MSFT", "Microsoft", ("Microsoft Corp", "Microsoft Corporation")),
    CompanyTarget("US", "NVDA", "NVIDIA", ("Nvidia Corp", "Nvidia Corporation")),
    CompanyTarget("US", "TSLA", "Tesla", ("Tesla Inc", "Tesla Motors")),
)

SAMPLE_GDELT_TIMESTAMPS = ("20240405000000", "20240522200000", "20240610170000")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="무료 뉴스·공시 point-in-time archive 수집")
    parser.add_argument("--source", choices=("ALL", "GDELT", "DART", "EDGAR", "NAVER", "GOOGLE"), default="ALL")
    parser.add_argument("--root", type=Path, default=DEFAULT_NEWS_ROOT)
    parser.add_argument("--sample", action="store_true", help="요청된 국내 3개/미국 4개 검증 표본")
    parser.add_argument("--market", choices=("KR", "US"))
    parser.add_argument("--symbol")
    parser.add_argument("--company-name")
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--gdelt-timestamp", action="append", default=[])
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--sec-user-agent", default=None)
    parser.add_argument("--max-records", type=int, default=50)
    return parser


def _targets(args: argparse.Namespace) -> tuple[CompanyTarget, ...]:
    if args.sample:
        return SAMPLE_TARGETS
    if not (args.market and args.symbol and args.company_name):
        raise ValueError("--sample이 아니면 --market, --symbol, --company-name이 필요합니다.")
    return (CompanyTarget(args.market, args.symbol, args.company_name, tuple(args.alias)),)


def _record_result(results: list[dict[str, object]], result: CollectionResult, *, label: str = "") -> None:
    results.append(
        {
            "label": label or result.source,
            "source": result.source,
            "status": result.status,
            "event_count": len(result.events),
            "raw_hashes": [item.data_hash for item in result.artifacts],
            "errors": result.errors,
            "notes": result.notes,
            "metadata": result.metadata,
        }
    )


def collect(args: argparse.Namespace) -> tuple[Path, list[dict[str, object]]]:
    store = NewsDataStore(args.root)
    targets = _targets(args)
    selected = {"GDELT", "DART", "EDGAR", "NAVER", "GOOGLE"} if args.source == "ALL" else {args.source}
    results: list[dict[str, object]] = []

    if "GDELT" in selected:
        client = GdeltBulkClient(store)
        timestamps = tuple(args.gdelt_timestamp) or (SAMPLE_GDELT_TIMESTAMPS if args.sample else ())
        if not timestamps:
            raise ValueError("GDELT 수집에는 --gdelt-timestamp가 필요합니다.")
        for timestamp in timestamps:
            _record_result(results, client.collect_company_sample(timestamp=timestamp, targets=targets), label=f"GDELT:{timestamp}")

    if "DART" in selected:
        client = OpenDartClient(store)
        mapping_rows, mapping_result = client.fetch_corp_codes()
        _record_result(results, mapping_result, label="DART:corp-codes")
        ticker_map = client.ticker_map(mapping_rows)
        for target in (item for item in targets if item.market == "KR"):
            corp = ticker_map.get(target.symbol.zfill(6))
            if not corp:
                unavailable = CollectionResult(source="dart", collected_at=mapping_result.collected_at)
                unavailable.errors.append(f"DART 종목 매핑 없음: {target.symbol}")
                _record_result(results, unavailable, label=f"DART:{target.symbol}")
                continue
            _record_result(
                results,
                client.search_filings(
                    corp_code=corp["corp_code"], symbol=target.symbol, company_name=target.company_name,
                    start_date=args.start_date, end_date=args.end_date,
                ),
                label=f"DART:{target.symbol}",
            )

    if "EDGAR" in selected:
        client = SecEdgarClient(store, user_agent=args.sec_user_agent)
        ticker_map, ticker_result = client.fetch_tickers()
        _record_result(results, ticker_result, label="EDGAR:ticker-map")
        for target in (item for item in targets if item.market == "US"):
            company = ticker_map.get(target.symbol.upper())
            if not company:
                unavailable = CollectionResult(source="edgar", collected_at=ticker_result.collected_at)
                unavailable.errors.append(f"SEC ticker/CIK 매핑 없음: {target.symbol}")
                _record_result(results, unavailable, label=f"EDGAR:{target.symbol}")
                continue
            _record_result(
                results,
                client.search_filings(
                    cik=company["cik"], ticker=target.symbol, company_name=target.company_name,
                    start_date=args.start_date,
                ),
                label=f"EDGAR:{target.symbol}",
            )

    if "NAVER" in selected:
        client = NaverNewsClient(store)
        for target in (item for item in targets if item.market == "KR"):
            _record_result(
                results,
                client.search(query=target.company_name, symbol=target.symbol, company_name=target.company_name, max_records=args.max_records),
                label=f"NAVER:{target.symbol}",
            )

    if "GOOGLE" in selected:
        client = GoogleNewsRssClient(store)
        for target in targets:
            query = " ".join((target.company_name, target.symbol, "stock company"))
            _record_result(
                results,
                client.search(
                    query=query, market=target.market, symbol=target.symbol,
                    company_name=target.company_name, max_records=args.max_records,
                ),
                label=f"GOOGLE:{target.market}:{target.symbol}",
            )

    source_tag = args.source.casefold()
    collection_id = (
        f"free-news-sample-{source_tag}-{date.today():%Y%m%d}"
        if args.sample
        else f"free-news-{source_tag}-{date.today():%Y%m%d}"
    )
    manifest_path = store.write_manifest(
        collection_id,
        {
            "purpose": "SOURCE_VALIDATION_ONLY" if args.sample else "FORWARD_NEWS_ARCHIVE",
            "scoring_connected": False,
            "credential_status": {
                name: "AVAILABLE" if os.getenv(name) else "MISSING"
                for name in (
                    "SEC_USER_AGENT", "DART_API_KEY",
                    "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET",
                )
            },
            "targets": [target.__dict__ for target in targets],
            "results": results,
        },
    )
    return manifest_path, results


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest_path, results = collect(args)
    except Exception as exc:
        parser.error(str(exc))
    print(json.dumps({"manifest": str(manifest_path), "results": results}, ensure_ascii=False, indent=2))
    return 2 if any(item["status"] == "unavailable" for item in results) else 0


if __name__ == "__main__":
    sys.exit(main())
