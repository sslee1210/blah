from __future__ import annotations

"""OpenDART official disclosure collector (data only; no sentiment scoring)."""

from datetime import timedelta
from io import BytesIO
import os
import re
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree

import requests

from ..models import CollectionResult, NormalizedEvent
from ..normalization import classify_event_type, iso_utc, stable_id
from ..storage import NewsDataStore
from .common import CachedHttpClient, utc_now


DART_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"


class OpenDartClient(CachedHttpClient):
    source = "dart"

    def __init__(
        self,
        store: NewsDataStore,
        *,
        api_key: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(store, session=session, timeout=timeout)
        self.api_key = api_key if api_key is not None else (os.getenv("DART_API_KEY") or os.getenv("OPENDART_API_KEY") or "")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def parse_corp_codes(payload: bytes) -> list[dict[str, str]]:
        try:
            with ZipFile(BytesIO(payload)) as archive:
                names = [name for name in archive.namelist() if name.casefold().endswith(".xml")]
                if not names:
                    raise ValueError("corpCode ZIP에 XML이 없습니다.")
                xml_payload = archive.read(names[0])
        except BadZipFile as exc:
            raise ValueError("corpCode 응답이 유효한 ZIP이 아닙니다.") from exc
        root = ElementTree.fromstring(xml_payload)
        rows: list[dict[str, str]] = []
        for node in root.findall(".//list"):
            row = {child.tag: (child.text or "").strip() for child in node}
            if row.get("corp_code"):
                rows.append(row)
        return rows

    @staticmethod
    def ticker_map(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
        return {str(row.get("stock_code", "")).zfill(6): row for row in rows if str(row.get("stock_code", "")).strip()}

    def fetch_corp_codes(self) -> tuple[list[dict[str, str]], CollectionResult]:
        collected_at = utc_now()
        result = CollectionResult(source=self.source, collected_at=collected_at)
        if not self.available:
            result.errors.append("DART_API_KEY/OPENDART_API_KEY 미설정")
            result.metadata["configured"] = False
            return [], result
        try:
            payload, artifact = self.get_bytes(
                self.source,
                DART_CORP_CODE_URL,
                params={"crtfc_key": self.api_key},
                suffix="zip",
                content_type="application/zip",
                cache_ttl=timedelta(days=1),
            )
            result.artifacts.append(artifact)
            rows = self.parse_corp_codes(payload)
        except Exception as exc:
            result.errors.append(f"OpenDART corpCode 실패: {str(exc)[:160]}")
            return [], result
        result.metadata.update({"configured": True, "corp_count": len(rows)})
        return rows, result

    def search_filings(
        self,
        *,
        corp_code: str,
        symbol: str,
        company_name: str,
        start_date: str,
        end_date: str,
        max_pages: int = 100,
    ) -> CollectionResult:
        collected_at = utc_now()
        result = CollectionResult(source=self.source, collected_at=collected_at)
        if not self.available:
            result.errors.append("DART_API_KEY/OPENDART_API_KEY 미설정")
            result.metadata["configured"] = False
            return result
        page = 1
        total_pages = 1
        while page <= min(total_pages, max_pages):
            params = {
                "crtfc_key": self.api_key,
                "corp_code": corp_code,
                "bgn_de": re.sub(r"\D", "", start_date),
                "end_de": re.sub(r"\D", "", end_date),
                "last_reprt_at": "N",
                "sort": "date",
                "sort_mth": "asc",
                "page_no": page,
                "page_count": 100,
            }
            try:
                payload, artifact = self.get_json(
                    self.source,
                    DART_LIST_URL,
                    params=params,
                    query=f"{symbol}:{start_date}:{end_date}:page={page}",
                    cache_ttl=timedelta(hours=6),
                )
                result.artifacts.append(artifact)
            except Exception as exc:
                result.errors.append(f"OpenDART 공시검색 실패(page={page}): {str(exc)[:160]}")
                break
            status = str(payload.get("status", ""))
            if status == "013":
                break
            if status != "000":
                result.errors.append(f"OpenDART status={status}: {payload.get('message', 'unknown')}")
                break
            total_pages = int(payload.get("total_page") or 1)
            for row in payload.get("list", []):
                if isinstance(row, dict):
                    event = self.normalize_filing(
                        row,
                        symbol=symbol,
                        company_name=company_name,
                        collected_at=collected_at,
                        raw_hash=artifact.data_hash,
                    )
                    if event:
                        result.events.append(event)
            page += 1
        result.events, processed = self.store.ingest_events(result.events, source=self.source)
        result.metadata.update(
            {
                "configured": True,
                "processed_path": str(processed or ""),
                "returned": len(result.events),
                "time_policy": "접수일만 제공되므로 해당 일 23:59:59 Asia/Seoul 이후 사용",
            }
        )
        return result

    @staticmethod
    def normalize_filing(
        row: dict[str, object], *, symbol: str, company_name: str, collected_at: str, raw_hash: str
    ) -> NormalizedEvent | None:
        receipt_number = str(row.get("rcept_no") or "").strip()
        receipt_date = str(row.get("rcept_dt") or "").strip()
        report_name = str(row.get("report_nm") or "").strip()
        if not receipt_number or not receipt_date or not report_name:
            return None
        # OpenDART list.json exposes a receipt date, not an intraday acceptance
        # time. End-of-day KST is a conservative no-same-day-use boundary.
        available_at = iso_utc(receipt_date, default_tz="Asia/Seoul", date_at_end=True)
        event_type = classify_event_type(report_name)
        correction = "정정" in report_name or report_name.startswith("[기재정정]")
        source_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_number}"
        return NormalizedEvent(
            event_id=stable_id("OPENDART", receipt_number, symbol, prefix="evt"),
            market="KR",
            symbol=symbol.upper().zfill(6),
            company_name=company_name or str(row.get("corp_name") or ""),
            event_type=event_type,
            event_time=available_at,
            first_seen_at=available_at,
            source_type="OPENDART",
            source_name="OpenDART",
            source_url=source_url,
            title=report_name,
            collected_at=collected_at,
            raw_reference={
                "raw_hash": raw_hash,
                "corp_code": str(row.get("corp_code") or ""),
                "stock_code": str(row.get("stock_code") or symbol),
                "receipt_number": receipt_number,
                "receipt_date": receipt_date,
                "corp_class": str(row.get("corp_cls") or ""),
                "filer_name": str(row.get("flr_nm") or ""),
                "remark": str(row.get("rm") or ""),
                "report_type": event_type,
                "correction": correction,
            },
            article_id=stable_id("OPENDART", receipt_number, prefix="filing"),
            event_time_precision="date",
            synthetic_time=True,
            pit_trust="PARTIALLY_TRUSTED",
            is_correction=correction,
            entities=(str(row.get("corp_name") or company_name),),
            source_event_type=report_name,
            normalized_event_type=event_type,
            source_level=1,
            scope="COMPANY",
            relevance_status="DIRECT_COMPANY",
        )
