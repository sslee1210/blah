from __future__ import annotations

"""KRX OPEN API adapter for date-keyed Korean equity evaluation data.

The adapter is deliberately isolated from the production analyzers.  KRX
daily prices are preserved as raw/as-traded observations.  Because the OPEN
API contract does not provide a verified split-adjusted return series here,
datasets produced by this module must remain outside the trusted performance
gate until a corporate-action source has been independently reconciled.
"""

from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
import os
import re
import time
from typing import Any, Iterable, Mapping

import pandas as pd
import requests

from .collector import ResumableCollector
from .models import CollectionRequest, DatasetManifest
from .storage import HistoricalDataStore


KRX_API_BASE = "https://data-dbg.krx.co.kr/svc/apis"
KRX_SERVICES = {
    "KOSPI_DAILY": "sto/stk_bydd_trd",
    "KOSDAQ_DAILY": "sto/ksq_bydd_trd",
    "KOSPI_BASIC": "sto/stk_isu_base_info",
    "KOSDAQ_BASIC": "sto/ksq_isu_base_info",
    "KOSPI_INDEX": "idx/kospi_dd_trd",
    "KOSDAQ_INDEX": "idx/kosdaq_dd_trd",
}
KRX_CREDENTIAL_NAMES = ("KRX_AUTH_KEY", "KRX_OPEN_API_KEY", "KRX_API_KEY", "AUTH_KEY")


class KrxOpenApiError(RuntimeError):
    """A sanitized KRX request or response-contract failure."""


@dataclass(frozen=True)
class KrxApiResponse:
    service: str
    base_date: str
    endpoint: str
    rows: tuple[dict[str, Any], ...]
    raw_payload: bytes
    data_hash: str


@dataclass
class KrxHistoricalBatch:
    universe_rows: list[dict[str, object]] = field(default_factory=list)
    stock_rows: list[dict[str, object]] = field(default_factory=list)
    index_rows: dict[str, list[dict[str, object]]] = field(
        default_factory=lambda: {"KOSPI": [], "KOSDAQ": []}
    )
    metadata_rows: dict[str, dict[str, object]] = field(default_factory=dict)
    responses: list[KrxApiResponse] = field(default_factory=list)

    def stock_frames(self) -> dict[str, pd.DataFrame]:
        return _frames(self.stock_rows, "symbol")

    def index_frames(self) -> dict[str, pd.DataFrame]:
        output: dict[str, pd.DataFrame] = {}
        for symbol, rows in self.index_rows.items():
            if rows:
                output[symbol] = _frame(rows)
        return output


def krx_auth_key_from_environment() -> str:
    for name in KRX_CREDENTIAL_NAMES:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


class KrxOpenApiClient:
    def __init__(
        self,
        auth_key: str | None = None,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        request_interval_seconds: float = 0.05,
    ) -> None:
        self.auth_key = krx_auth_key_from_environment() if auth_key is None else auth_key.strip()
        self.session = session or requests.Session()
        self.timeout = timeout
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self._last_request_at = 0.0

    @property
    def available(self) -> bool:
        return bool(self.auth_key)

    def fetch(self, service: str, base_date: str | date | pd.Timestamp) -> KrxApiResponse:
        if service not in KRX_SERVICES:
            raise ValueError(f"지원하지 않는 KRX service: {service}")
        if not self.available:
            raise KrxOpenApiError(
                "KRX 인증키가 없습니다. KRX_AUTH_KEY 환경변수와 서비스별 활용 승인을 확인하십시오."
            )
        day = _base_date(base_date)
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self.request_interval_seconds:
            time.sleep(self.request_interval_seconds - elapsed)
        endpoint = f"{KRX_API_BASE}/{KRX_SERVICES[service]}"
        try:
            response = self.session.get(
                endpoint,
                params={"basDd": day},
                headers={"AUTH_KEY": self.auth_key, "Accept": "application/json"},
                timeout=self.timeout,
            )
            self._last_request_at = time.monotonic()
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "unknown")
            raise KrxOpenApiError(f"KRX OPEN API HTTP {status}: {service} {day}") from exc
        except (requests.RequestException, ValueError, TypeError) as exc:
            raise KrxOpenApiError(
                f"KRX OPEN API 응답 실패: {service} {day} ({type(exc).__name__})"
            ) from exc
        rows = payload.get("OutBlock_1") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise KrxOpenApiError(f"KRX OPEN API OutBlock_1 누락: {service} {day}")
        normalized_rows = tuple(row for row in rows if isinstance(row, dict))
        raw = bytes(getattr(response, "content", b"")) or json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        return KrxApiResponse(
            service=service,
            base_date=day,
            endpoint=endpoint,
            rows=normalized_rows,
            raw_payload=raw,
            data_hash=hashlib.sha256(raw).hexdigest(),
        )


class KrxHistoricalAdapter:
    """Collect complete date cross-sections and adapt them to evaluator tables."""

    requests_per_session = 6
    price_adjustment = "raw_unadjusted"
    corporate_action_policy = (
        "unverified; KRX daily raw prices have no reconciled split factor or adjusted outcome series"
    )
    survivorship_safe = False
    point_in_time_level = (
        "official date-keyed KRX cross-sections; completeness pending live delisting validation"
    )

    def __init__(self, client: KrxOpenApiClient) -> None:
        self.client = client

    def collect_dates(self, dates: Iterable[str | date | pd.Timestamp]) -> KrxHistoricalBatch:
        batch = KrxHistoricalBatch()
        for value in dates:
            day = _base_date(value)
            for market in ("KOSPI", "KOSDAQ"):
                daily = self.client.fetch(f"{market}_DAILY", day)
                basic = self.client.fetch(f"{market}_BASIC", day)
                index = self.client.fetch(f"{market}_INDEX", day)
                batch.responses.extend((daily, basic, index))
                universe, prices, metadata = normalize_stock_cross_section(
                    market=market,
                    base_date=day,
                    daily_rows=daily.rows,
                    basic_rows=basic.rows,
                )
                batch.universe_rows.extend(universe)
                batch.stock_rows.extend(prices)
                for row in metadata:
                    batch.metadata_rows[str(row["symbol"])] = row
                batch.index_rows[market].append(
                    normalize_broad_market_index(market, day, index.rows)
                )
        return batch


def normalize_stock_cross_section(
    *,
    market: str,
    base_date: str | date | pd.Timestamp,
    daily_rows: Iterable[Mapping[str, Any]],
    basic_rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    day = _base_date(base_date)
    iso_day = pd.Timestamp(day).date().isoformat()
    market = market.upper()
    if market not in {"KOSPI", "KOSDAQ"}:
        raise ValueError("market은 KOSPI 또는 KOSDAQ이어야 합니다.")
    daily_by_symbol = {
        symbol: dict(row)
        for row in daily_rows
        if (symbol := _symbol(row))
    }
    basic_by_symbol = {
        symbol: dict(row)
        for row in basic_rows
        if (symbol := _symbol(row))
    }
    symbols = sorted(set(basic_by_symbol) | set(daily_by_symbol))
    preliminary: list[dict[str, object]] = []
    prices: list[dict[str, object]] = []
    metadata: list[dict[str, object]] = []
    for symbol in symbols:
        basic = basic_by_symbol.get(symbol, {})
        daily = daily_by_symbol.get(symbol, {})
        name = str(
            basic.get("ISU_ABBRV")
            or basic.get("ISU_NM")
            or daily.get("ISU_NM")
            or symbol
        ).strip()
        security_type = _security_type(basic, name)
        listing_date = _optional_date(basic.get("LIST_DD"))
        permanent_id = _permanent_id(basic, market, symbol)
        permanent_id_verified = not permanent_id.startswith("KRX:")
        eligible = security_type == "common_stock"
        exclusion_reason = "" if eligible else f"security_type={security_type}"
        if listing_date and iso_day < listing_date:
            eligible = False
            exclusion_reason = "before_listing_date"
        trade_value = _number(daily.get("ACC_TRDVAL"))
        market_cap = _number(daily.get("MKTCAP"))
        preliminary.append(
            {
                "date": iso_day,
                "market": "KR",
                "exchange": market,
                "symbol": symbol,
                "permanent_id": permanent_id,
                "permanent_id_verified": permanent_id_verified,
                "company_name": name,
                "security_type": security_type,
                "listing_date": listing_date or "",
                "delisting_date": "",
                "eligible": eligible,
                "exclusion_reason": exclusion_reason,
                "trade_value": trade_value,
                "market_cap": market_cap,
                "source": "KRX_OPEN_API",
                "point_in_time_level": "official_date_cross_section",
            }
        )
        metadata.append(
            {
                "symbol": symbol,
                "permanent_id": permanent_id,
                "permanent_id_verified": permanent_id_verified,
                "exchange": market,
                "name": name,
                "korean_name": name,
                "english_name": str(basic.get("ISU_ENG_NM") or ""),
                "sector": str(basic.get("SECT_TP_NM") or daily.get("SECT_TP_NM") or ""),
                "security_type": security_type,
                "is_etf": False,
                "listing_date": listing_date or "",
                "delisting_date": "",
            }
        )
        if eligible and daily:
            price = _price_row(iso_day, symbol, daily)
            if price is not None:
                prices.append(price)

    ranked = sorted(
        (row for row in preliminary if row["eligible"] and float(row["trade_value"]) > 0),
        key=lambda row: (-float(row["trade_value"]), str(row["symbol"])),
    )
    ranks = {str(row["symbol"]): rank for rank, row in enumerate(ranked, 1)}
    universe = [{**row, "trade_value_rank": ranks.get(str(row["symbol"]), "")} for row in preliminary]
    return universe, prices, metadata


def normalize_broad_market_index(
    market: str,
    base_date: str | date | pd.Timestamp,
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, object]:
    market = market.upper()
    candidates = [dict(row) for row in rows]
    aliases = {
        "KOSPI": {"KOSPI", "코스피", "종합주가지수"},
        "KOSDAQ": {"KOSDAQ", "코스닥", "코스닥종합"},
    }[market]
    selected = next(
        (
            row
            for row in candidates
            if str(row.get("IDX_NM") or row.get("IDX_NM_ENG") or "").strip() in aliases
        ),
        None,
    )
    if selected is None:
        raise KrxOpenApiError(f"{market} 대표지수를 KRX 응답에서 찾지 못했습니다: {_base_date(base_date)}")
    values = {
        "date": pd.Timestamp(_base_date(base_date)).date().isoformat(),
        "open": _number(selected.get("OPNPRC_IDX")),
        "high": _number(selected.get("HGPRC_IDX")),
        "low": _number(selected.get("LWPRC_IDX")),
        "close": _number(selected.get("CLSPRC_IDX")),
        "volume": _number(selected.get("ACC_TRDVOL")),
        "trade_value": _number(selected.get("ACC_TRDVAL")),
    }
    if min(float(values[name]) for name in ("open", "high", "low", "close")) <= 0:
        raise KrxOpenApiError(f"{market} 대표지수 OHLC가 유효하지 않습니다: {_base_date(base_date)}")
    return values


def audit_historical_membership(universe_rows: Iterable[Mapping[str, object]]) -> dict[str, object]:
    rows = [dict(row) for row in universe_rows if bool(row.get("eligible"))]
    dates = sorted({str(row.get("date")) for row in rows if row.get("date")})
    by_date = {
        day: {str(row["symbol"]) for row in rows if str(row.get("date")) == day}
        for day in dates
    }
    latest = by_date.get(dates[-1], set()) if dates else set()
    earlier = set().union(*(by_date[day] for day in dates[:-1])) if len(dates) > 1 else set()
    before_listing = [
        str(row.get("symbol"))
        for row in rows
        if row.get("listing_date") and str(row.get("date")) < str(row.get("listing_date"))
    ]
    after_delisting = [
        str(row.get("symbol"))
        for row in rows
        if row.get("delisting_date") and str(row.get("date")) > str(row.get("delisting_date"))
    ]
    return {
        "dates": dates,
        "snapshot_count": len(dates),
        "historical_only_symbols": sorted(earlier - latest),
        "new_on_latest_symbols": sorted(latest - earlier),
        "before_listing_violations": sorted(set(before_listing)),
        "after_delisting_violations": sorted(set(after_delisting)),
    }


def estimated_request_count(session_count: int) -> int:
    return max(0, int(session_count)) * KrxHistoricalAdapter.requests_per_session


def publish_krx_batch(
    store: HistoricalDataStore,
    batch: KrxHistoricalBatch,
    *,
    resume: bool = True,
) -> DatasetManifest:
    """Publish KRX cross-sections using the existing evaluator dataset contract."""

    for response in batch.responses:
        store.write_source_snapshot(
            source="krx_open_api",
            category=response.service.casefold(),
            base_date=response.base_date,
            payload=response.raw_payload,
        )
    universe_path = store.write_universe_snapshots(batch.universe_rows)
    frames: dict[str, pd.DataFrame] = {}
    requests_: list[CollectionRequest] = []
    for symbol, frame in batch.stock_frames().items():
        metadata = batch.metadata_rows.get(symbol, {})
        request = CollectionRequest(
            market="KR",
            symbol=symbol,
            exchange=str(metadata.get("exchange") or "KRX"),
            name=str(metadata.get("name") or symbol),
            sector=str(metadata.get("sector") or ""),
            security_type=str(metadata.get("security_type") or "common_stock"),
            listing_date=str(metadata.get("listing_date") or "") or None,
        )
        requests_.append(request)
        frames[request.key] = frame
    for symbol, frame in batch.index_frames().items():
        request = CollectionRequest(
            market="KR",
            symbol=symbol,
            exchange=symbol,
            kind="index",
            name=symbol,
            security_type="market_index",
        )
        requests_.append(request)
        frames[request.key] = frame
    provider = _KrxBatchProvider(frames)
    manifest = ResumableCollector(store, provider, max_attempts=1).collect(
        requests_,
        resume=resume,
        universe_snapshot_path=str(universe_path.relative_to(store.project_root)),
    )
    store.write_stock_metadata("KR", batch.metadata_rows.values())
    return manifest


class _KrxBatchProvider:
    source_name = "krx_open_api"
    source_url = KRX_API_BASE
    license_scope = "KRX OPEN API approved internal/non-commercial use; redistribution restricted"
    timezone_by_market = {"KR": "Asia/Seoul"}
    price_adjustment = KrxHistoricalAdapter.price_adjustment
    corporate_action_policy = KrxHistoricalAdapter.corporate_action_policy
    survivorship_safe = False
    point_in_time_level = KrxHistoricalAdapter.point_in_time_level
    limitations = (
        "상장폐지 종목 포함 여부를 실제 과거/현재 KRX 응답 사례로 검증하기 전입니다.",
        "KRX raw 일별가격과 split-adjusted outcome을 연결할 corporate-action 원장이 없습니다.",
        "따라서 이 데이터셋은 TECHNICAL_BASELINE 연결 검증용이며 성능 주장에 사용할 수 없습니다.",
    )

    def __init__(self, frames: Mapping[str, pd.DataFrame]) -> None:
        self.frames = frames

    def fetch_daily(self, request: CollectionRequest) -> pd.DataFrame:
        try:
            return self.frames[request.key].copy()
        except KeyError as exc:
            raise ValueError(f"KRX batch에 가격 frame이 없습니다: {request.key}") from exc


def _price_row(day: str, symbol: str, row: Mapping[str, Any]) -> dict[str, object] | None:
    values = {
        "date": day,
        "symbol": symbol,
        "open": _number(row.get("TDD_OPNPRC")),
        "high": _number(row.get("TDD_HGPRC")),
        "low": _number(row.get("TDD_LWPRC")),
        "close": _number(row.get("TDD_CLSPRC")),
        "volume": _number(row.get("ACC_TRDVOL")),
        "trade_value": _number(row.get("ACC_TRDVAL")),
        "market_cap": _number(row.get("MKTCAP")),
        "listed_shares": _number(row.get("LIST_SHRS")),
    }
    if min(float(values[name]) for name in ("open", "high", "low", "close")) <= 0:
        return None
    return values


def _security_type(row: Mapping[str, Any], name: str) -> str:
    kind = " ".join(
        str(row.get(key) or "")
        for key in ("KIND_STKCERT_TP_NM", "STK_KIND", "SECUGRP_NM")
    ).casefold()
    if "우선" in kind or "preferred" in kind or re.search(r"우(?:b|c)?$", name, re.I):
        return "preferred_stock"
    if "보통" in kind or "common" in kind:
        return "common_stock"
    return "unknown_stock_type"


def _symbol(row: Mapping[str, Any]) -> str:
    for key in ("ISU_SRT_CD", "ISU_CD", "SHORT_CODE"):
        value = re.sub(r"\s+", "", str(row.get(key) or "")).upper()
        if re.fullmatch(r"\d{6}", value):
            return value
        if re.fullmatch(r"KR[A-Z0-9]{10}", value):
            candidate = value[3:9]
            if re.fullmatch(r"\d{6}", candidate):
                return candidate
    return ""


def _permanent_id(row: Mapping[str, Any], market: str, symbol: str) -> str:
    value = str(row.get("ISU_CD") or "").strip().upper()
    if re.fullmatch(r"KR[A-Z0-9]{10}", value):
        return value
    return f"KRX:{market}:{symbol}"


def _optional_date(value: Any) -> str | None:
    text = re.sub(r"\D", "", str(value or ""))
    if len(text) != 8:
        return None
    try:
        return pd.Timestamp(text).date().isoformat()
    except ValueError:
        return None


def _base_date(value: str | date | pd.Timestamp) -> str:
    try:
        return pd.Timestamp(value).strftime("%Y%m%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"유효하지 않은 KRX 기준일: {value!r}") from exc


def _number(value: Any) -> float:
    text = str(value or "0").replace(",", "").strip()
    try:
        return float(text or 0)
    except ValueError:
        return 0.0


def _frames(rows: Iterable[Mapping[str, object]], key: str) -> dict[str, pd.DataFrame]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row[key]), []).append(dict(row))
    return {name: _frame(values, drop=(key,)) for name, values in grouped.items()}


def _frame(rows: Iterable[Mapping[str, object]], *, drop: tuple[str, ...] = ()) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("date"), errors="raise"))
    frame.index.name = "timestamp"
    for column in drop:
        if column in frame:
            frame = frame.drop(columns=column)
    return frame.sort_index()
