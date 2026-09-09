from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from data_pipeline.collector import ResumableCollector
from data_pipeline.models import CollectionRequest
from data_pipeline.calendars import exchange_sessions, session_status
from data_pipeline.point_in_time import SecurityHistory, eligible_on, point_in_time_slice, price_views
from data_pipeline.quality import inspect_daily_prices
from data_pipeline.storage import HistoricalDataStore, sha256_file
import data_pipeline.storage as historical_storage
from data_pipeline.krx_open_api import (
    KRX_API_BASE,
    KrxApiResponse,
    KrxHistoricalBatch,
    KrxOpenApiClient,
    KrxOpenApiError,
    audit_historical_membership,
    normalize_broad_market_index,
    normalize_stock_cross_section,
    publish_krx_batch,
)
from evaluate_analyzer import _load_dataset, _manifest_trust


def _daily(periods: int = 90, start: str = "2024-01-02") -> pd.DataFrame:
    index = pd.bdate_range(start, periods=periods)
    close = np.linspace(100.0, 120.0, periods)
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(periods, 10_000.0),
            "trade_value": close * 10_000.0,
        },
        index=index,
    )


class _FakeProvider:
    source_name = "fake"
    source_url = "https://example.invalid"
    license_scope = "test"
    timezone_by_market = {"US": "America/New_York", "KR": "Asia/Seoul"}
    price_adjustment = "raw_and_split_adjusted"
    corporate_action_policy = "raw signal; split-adjusted outcome"
    survivorship_safe = True
    point_in_time_level = "historical daily snapshots"
    limitations: tuple[str, ...] = ()

    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.calls: dict[str, int] = {}

    def fetch_daily(self, request: CollectionRequest) -> pd.DataFrame:
        self.calls[request.key] = self.calls.get(request.key, 0) + 1
        if request.symbol in self.failures:
            raise TimeoutError(f"forced failure: {request.symbol}")
        return _daily()


def test_as_of_cutoff_blocks_all_future_rows() -> None:
    frame = _daily(10)
    result = point_in_time_slice(frame, as_of=frame.index[4], timezone="America/New_York")
    assert result.index.max() == frame.index[4]
    assert len(result) == 5


def test_listing_and_delisting_boundaries_exclude_security() -> None:
    history = SecurityHistory("P1", "NEW", "NYSE", "common_stock", "2024-02-01", "2024-06-30")
    assert not eligible_on(history, "2024-01-31")
    assert eligible_on(history, "2024-02-01")
    assert eligible_on(history, "2024-06-30")
    assert not eligible_on(history, "2024-07-01")


def test_ticker_history_boundary_is_point_in_time() -> None:
    old = SecurityHistory("P1", "OLD", "NYSE", "common_stock", ticker_end_date="2024-03-31")
    new = SecurityHistory("P1", "NEW", "NYSE", "common_stock", ticker_start_date="2024-04-01")
    assert eligible_on(old, "2024-03-31") and not eligible_on(old, "2024-04-01")
    assert not eligible_on(new, "2024-03-31") and eligible_on(new, "2024-04-01")


def test_duplicate_rows_are_reported_not_silently_fixed() -> None:
    frame = _daily(5)
    duplicate = pd.concat([frame, frame.iloc[[2]]])
    report = inspect_daily_prices(
        duplicate,
        market="US",
        symbol="DUP",
        timezone_name="America/New_York",
    )
    assert any(item.code == "duplicate_session" and item.severity == "error" for item in report.issues)
    assert len(duplicate) == 6


def test_atomic_write_retries_transient_permission_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "checkpoint.json"
    real_replace = Path.replace
    calls = {"count": 0}

    def flaky_replace(self: Path, target: Path):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError("transient scanner lock")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(historical_storage.time, "sleep", lambda _: None)

    historical_storage._atomic_write(path, b"ok")

    assert path.read_bytes() == b"ok"
    assert calls["count"] == 3


def test_raw_prices_feed_signals_and_adjusted_prices_feed_outcomes() -> None:
    frame = _daily(3)
    for name in ("open", "high", "low", "close"):
        frame[f"raw_{name}"] = frame[name]
        frame[f"adjusted_{name}"] = frame[name] / 2.0
    frame["raw_volume"] = frame["volume"]
    frame["adjusted_volume"] = frame["volume"] * 2.0
    signal, outcome, policy = price_views(frame)
    assert signal.iloc[0]["close"] == frame.iloc[0]["raw_close"]
    assert outcome.iloc[0]["close"] == frame.iloc[0]["adjusted_close"]
    assert policy == "raw_for_signal_adjusted_for_outcome"


def test_adjustment_factor_mismatch_is_rejected() -> None:
    frame = _daily(3)
    for name in ("open", "high", "low", "close"):
        frame[f"raw_{name}"] = frame[name]
        frame[f"adjusted_{name}"] = frame[name] / 2.0
    frame["adjustment_factor"] = [0.5, 0.4, 0.5]
    report = inspect_daily_prices(
        frame,
        market="US",
        symbol="SPLIT",
        timezone_name="America/New_York",
    )
    assert any(
        item.code == "adjustment_factor_mismatch" and item.severity == "error"
        for item in report.issues
    )


def test_stale_and_invalid_ohlc_are_quality_errors() -> None:
    frame = _daily(5)
    frame.iloc[2, frame.columns.get_loc("high")] = frame.iloc[2]["low"] - 1
    report = inspect_daily_prices(
        frame,
        market="KR",
        symbol="BAD",
        timezone_name="Asia/Seoul",
        stale_after="2025-01-01",
    )
    codes = {item.code for item in report.issues if item.severity == "error"}
    assert {"invalid_ohlc_relation", "stale_data"}.issubset(codes)


def test_timezone_cutoff_uses_market_local_date() -> None:
    index = pd.DatetimeIndex(["2024-03-11T03:00:00Z", "2024-03-12T03:00:00Z"])
    frame = _daily(2)
    frame.index = index
    result = point_in_time_slice(frame, as_of="2024-03-10", timezone="America/New_York")
    assert len(result) == 1


def test_known_holiday_is_not_reported_as_missing_session() -> None:
    frame = _daily(2)
    frame.index = pd.DatetimeIndex(["2024-12-24", "2024-12-26"])
    report = inspect_daily_prices(
        frame,
        market="US",
        symbol="HOLIDAY",
        timezone_name="America/New_York",
        expected_sessions=["2024-12-24", "2024-12-26"],
    )
    assert not any(item.code == "missing_exchange_session" for item in report.issues)


def test_real_exchange_calendars_distinguish_holiday_and_early_close() -> None:
    assert session_status("US", "2024-12-25") == "CLOSED"
    assert session_status("US", "2024-07-03") == "EARLY_CLOSE"
    assert session_status("US", "2024-07-05") == "REGULAR"
    assert session_status("KR", "2024-12-25") == "CLOSED"
    assert session_status("KR", "2026-06-03") == "CLOSED"
    assert session_status("KR", "2026-07-17") == "CLOSED"
    result = exchange_sessions("KR", "2024-12-23", "2024-12-27")
    assert pd.Timestamp("2024-12-25") not in result.sessions
    assert result.calendar_name == "XKRX"


def test_collector_resume_skips_hash_verified_success(tmp_path: Path) -> None:
    store = HistoricalDataStore(tmp_path, "resume_test")
    provider = _FakeProvider()
    request = CollectionRequest("US", "AAA", "NYSE")
    collector = ResumableCollector(store, provider, max_attempts=1)
    first = collector.collect([request])
    second = collector.collect([request])
    assert provider.calls[request.key] == 1
    assert first.assets[0].data_hash == second.assets[0].data_hash
    raw = tmp_path / second.assets[0].raw_path
    assert sha256_file(raw) == second.assets[0].data_hash


def test_api_failure_is_partial_and_success_is_preserved(tmp_path: Path) -> None:
    store = HistoricalDataStore(tmp_path, "partial_test")
    provider = _FakeProvider({"FAIL"})
    requests = [CollectionRequest("US", "GOOD", "NYSE"), CollectionRequest("US", "FAIL", "NYSE")]
    manifest = ResumableCollector(store, provider, max_attempts=2).collect(requests)
    assert manifest.status == "partial"
    assert [item.symbol for item in manifest.assets] == ["GOOD"]
    assert store.failure_path.exists()
    assert (store.dataset_root / "US" / "stocks" / "GOOD.csv").exists()


def test_manifest_contains_required_fields_and_trust_gate(tmp_path: Path) -> None:
    store = HistoricalDataStore(tmp_path, "manifest_test")
    request = CollectionRequest("KR", "005930", "KOSPI")
    universe_path = "evaluation/datasets/manifest_test/universe.csv"
    manifest = ResumableCollector(store, _FakeProvider(), max_attempts=1).collect(
        [request], universe_snapshot_path=universe_path
    )
    payload = manifest.to_dict()
    payload["corporate_action_verified"] = True
    asset = payload["assets"][0]
    required = {
        "source", "market", "symbol", "security_type", "start_date", "end_date", "rows",
        "collected_at", "timezone", "price_adjustment", "corporate_action_policy",
        "survivorship_safe", "point_in_time_level", "missing_days", "duplicate_rows", "data_hash",
    }
    assert required.issubset(asset)
    point_in_time, prices = _manifest_trust(payload)
    assert point_in_time and prices


def test_failed_quality_is_raw_only_not_published(tmp_path: Path) -> None:
    class DuplicateProvider(_FakeProvider):
        def fetch_daily(self, request: CollectionRequest) -> pd.DataFrame:
            frame = _daily(5)
            return pd.concat([frame, frame.iloc[[2]]])

    store = HistoricalDataStore(tmp_path, "raw_only")
    manifest = ResumableCollector(store, DuplicateProvider(), max_attempts=1).collect(
        [CollectionRequest("US", "DUP", "NYSE")]
    )
    asset = manifest.assets[0]
    assert asset.quality_status == "failed"
    assert (tmp_path / asset.raw_path).exists()
    assert asset.processed_path == ""
    assert not (store.dataset_root / "US" / "stocks" / "DUP.csv").exists()


def test_krx_client_requires_auth_and_uses_official_header_contract(tmp_path: Path) -> None:
    with pytest.raises(KrxOpenApiError, match="인증키"):
        KrxOpenApiClient(auth_key="").fetch("KOSPI_DAILY", "2024-06-03")

    class Response:
        content = b'{"OutBlock_1":[]}'

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"OutBlock_1": []}

    class Session:
        def __init__(self) -> None:
            self.url = ""
            self.kwargs: dict[str, object] = {}

        def get(self, url: str, **kwargs):
            self.url = url
            self.kwargs = kwargs
            return Response()

    session = Session()
    response = KrxOpenApiClient(
        auth_key="configured", session=session, request_interval_seconds=0
    ).fetch("KOSPI_DAILY", "2024-06-03")

    assert session.url == f"{KRX_API_BASE}/sto/stk_bydd_trd"
    assert session.kwargs["params"] == {"basDd": "20240603"}
    assert session.kwargs["headers"] == {
        "AUTH_KEY": "configured",
        "Accept": "application/json",
    }
    assert response.rows == ()


def test_krx_cross_section_reconstructs_common_stock_trade_value_rank() -> None:
    daily = [
        _krx_daily("005930", "삼성전자", 100_000_000),
        _krx_daily("000660", "SK하이닉스", 200_000_000),
        _krx_daily("005935", "삼성전자우", 300_000_000),
    ]
    basic = [
        _krx_basic("KR7005930003", "005930", "삼성전자", "19750611", "보통주"),
        _krx_basic("KR7000660001", "000660", "SK하이닉스", "19961226", "보통주"),
        _krx_basic("KR7005931001", "005935", "삼성전자우", "19890328", "우선주"),
    ]
    universe, prices, metadata = normalize_stock_cross_section(
        market="KOSPI", base_date="2024-06-03", daily_rows=daily, basic_rows=basic
    )

    by_symbol = {str(row["symbol"]): row for row in universe}
    assert by_symbol["000660"]["trade_value_rank"] == 1
    assert by_symbol["005930"]["trade_value_rank"] == 2
    assert by_symbol["005935"]["eligible"] is False
    assert by_symbol["005930"]["permanent_id"] == "KR7005930003"
    assert {str(row["symbol"]) for row in prices} == {"000660", "005930"}
    assert {str(row["symbol"]) for row in metadata} == {"000660", "005930", "005935"}


def test_krx_membership_detects_historical_only_and_listing_boundaries() -> None:
    rows = [
        {"date": "2020-01-02", "symbol": "OLD", "eligible": True, "listing_date": "1999-01-01", "delisting_date": ""},
        {"date": "2020-01-02", "symbol": "STAY", "eligible": True, "listing_date": "1999-01-01", "delisting_date": ""},
        {"date": "2024-06-03", "symbol": "STAY", "eligible": True, "listing_date": "1999-01-01", "delisting_date": ""},
        {"date": "2024-06-03", "symbol": "NEW", "eligible": True, "listing_date": "2024-06-03", "delisting_date": ""},
    ]
    audit = audit_historical_membership(rows)

    assert audit["historical_only_symbols"] == ["OLD"]
    assert audit["new_on_latest_symbols"] == ["NEW"]
    assert audit["before_listing_violations"] == []
    assert audit["after_delisting_violations"] == []


def test_krx_broad_market_index_is_adapted_to_ohlcv() -> None:
    row = normalize_broad_market_index(
        "KOSPI",
        "2024-06-03",
        [
            {
                "IDX_NM": "코스피",
                "OPNPRC_IDX": "2700.00",
                "HGPRC_IDX": "2720.00",
                "LWPRC_IDX": "2690.00",
                "CLSPRC_IDX": "2710.00",
                "ACC_TRDVOL": "1000000",
                "ACC_TRDVAL": "9000000000",
            }
        ],
    )
    assert row["date"] == "2024-06-03"
    assert row["close"] == 2710.0
    assert row["trade_value"] == 9_000_000_000.0


def test_krx_batch_publishes_existing_evaluator_contract_but_stays_untrusted(tmp_path: Path) -> None:
    batch = KrxHistoricalBatch(
        universe_rows=[
            {
                "date": "2024-06-03", "market": "KR", "symbol": "005930",
                "eligible": True, "source": "KRX_OPEN_API",
                "point_in_time_level": "official_date_cross_section",
                "permanent_id": "KR7005930003", "trade_value_rank": 1,
            }
        ],
        stock_rows=[
            {
                "date": "2024-06-03", "symbol": "005930", "open": 100.0,
                "high": 110.0, "low": 95.0, "close": 105.0, "volume": 1000.0,
                "trade_value": 105000.0, "market_cap": 1_000_000.0,
                "listed_shares": 10_000.0,
            }
        ],
        index_rows={
            "KOSPI": [{"date": "2024-06-03", "open": 2700.0, "high": 2720.0, "low": 2690.0, "close": 2710.0, "volume": 1000.0, "trade_value": 100000.0}],
            "KOSDAQ": [{"date": "2024-06-03", "open": 850.0, "high": 860.0, "low": 845.0, "close": 855.0, "volume": 1000.0, "trade_value": 100000.0}],
        },
        metadata_rows={
            "005930": {
                "symbol": "005930", "permanent_id": "KR7005930003",
                "permanent_id_verified": True, "exchange": "KOSPI",
                "name": "삼성전자", "korean_name": "삼성전자", "english_name": "Samsung Electronics",
                "sector": "전기전자", "security_type": "common_stock", "is_etf": False,
                "listing_date": "1975-06-11", "delisting_date": "",
            }
        },
        responses=[
            KrxApiResponse(
                "KOSPI_DAILY", "20240603", f"{KRX_API_BASE}/sto/stk_bydd_trd",
                (), b'{"OutBlock_1":[]}', "unused-by-writer",
            )
        ],
    )
    store = HistoricalDataStore(tmp_path, "krx_adapter")
    manifest = publish_krx_batch(store, batch, resume=False)
    stocks, proxies = _load_dataset(store.dataset_root, "KR")
    universe = pd.read_csv(store.dataset_root / "universe.csv", dtype={"symbol": str})

    assert set(stocks) == {"005930"}
    assert set(proxies) == {"KOSPI", "KOSDAQ"}
    assert universe.iloc[0]["permanent_id"] == "KR7005930003"
    assert universe.iloc[0]["trade_value_rank"] == 1
    assert manifest.survivorship_safe is True
    assert manifest.delisted_securities_included is False
    assert manifest.corporate_action_verified is False
    assert manifest.trust_level == "EXPLORATORY"
    assert list((tmp_path / "data" / "raw" / "kr" / "krx_open_api" / "snapshots").rglob("*.json"))


def _krx_daily(symbol: str, name: str, trade_value: int) -> dict[str, str]:
    return {
        "ISU_CD": symbol,
        "ISU_NM": name,
        "TDD_OPNPRC": "100",
        "TDD_HGPRC": "110",
        "TDD_LWPRC": "90",
        "TDD_CLSPRC": "105",
        "ACC_TRDVOL": "1000",
        "ACC_TRDVAL": str(trade_value),
        "MKTCAP": "1000000000",
        "LIST_SHRS": "10000000",
    }


def _krx_basic(isin: str, symbol: str, name: str, listing_date: str, kind: str) -> dict[str, str]:
    return {
        "ISU_CD": isin,
        "ISU_SRT_CD": symbol,
        "ISU_NM": name,
        "ISU_ABBRV": name,
        "ISU_ENG_NM": name,
        "LIST_DD": listing_date,
        "KIND_STKCERT_TP_NM": kind,
        "SECUGRP_NM": "주권",
        "SECT_TP_NM": "테스트업종",
    }
