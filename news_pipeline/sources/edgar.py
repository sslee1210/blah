from __future__ import annotations

"""SEC data.sec.gov submissions collector."""

from datetime import timedelta
import os
import re
from typing import Any, Iterable

import requests

from ..models import CollectionResult, NormalizedEvent
from ..normalization import classify_event_type, iso_utc, stable_id
from ..storage import NewsDataStore
from .common import CachedHttpClient, utc_now


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_SUBMISSION_FILE_URL = "https://data.sec.gov/submissions/{name}"


class SecEdgarClient(CachedHttpClient):
    source = "edgar"

    def __init__(
        self,
        store: NewsDataStore,
        *,
        user_agent: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ) -> None:
        configured = user_agent if user_agent is not None else os.getenv("SEC_USER_AGENT", "")
        effective = configured or "Real-Stock-Analyzer/1.0 local-research"
        super().__init__(store, session=session, timeout=timeout, user_agent=effective)
        self.user_agent_configured = bool(configured)

    @property
    def available(self) -> bool:
        return self.user_agent_configured

    @staticmethod
    def ticker_map(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
        output: dict[str, dict[str, str]] = {}
        for row in payload.values():
            if not isinstance(row, dict) or not row.get("ticker") or row.get("cik_str") is None:
                continue
            ticker = str(row["ticker"]).upper()
            output[ticker] = {
                "ticker": ticker,
                "cik": str(row["cik_str"]).zfill(10),
                "title": str(row.get("title") or ""),
            }
        return output

    def fetch_tickers(self) -> tuple[dict[str, dict[str, str]], CollectionResult]:
        collected_at = utc_now()
        result = CollectionResult(source=self.source, collected_at=collected_at)
        if not self.available:
            result.errors.append("SEC_USER_AGENT 미설정: 조직/이름과 연락처를 포함한 식별값이 필요합니다.")
            result.metadata["configured"] = False
            return {}, result
        try:
            payload, artifact = self.get_json(
                self.source,
                SEC_TICKERS_URL,
                cache_ttl=timedelta(days=1),
            )
            result.artifacts.append(artifact)
            mapping = self.ticker_map(payload)
        except Exception as exc:
            result.errors.append(f"SEC ticker/CIK mapping 실패: {str(exc)[:160]}")
            return {}, result
        if not self.user_agent_configured:
            result.notes.append("SEC_USER_AGENT 미설정: 지속 수집 전 조직/연락처를 포함한 User-Agent 설정 필요")
        result.metadata["ticker_count"] = len(mapping)
        return mapping, result

    def search_filings(
        self,
        *,
        cik: str,
        ticker: str,
        company_name: str,
        forms: Iterable[str] = ("8-K", "10-Q", "10-K", "6-K"),
        start_date: str = "2015-01-01",
        include_history_files: bool = True,
    ) -> CollectionResult:
        collected_at = utc_now()
        result = CollectionResult(source=self.source, collected_at=collected_at)
        if not self.available:
            result.errors.append("SEC_USER_AGENT 미설정: 조직/이름과 연락처를 포함한 식별값이 필요합니다.")
            result.metadata["configured"] = False
            return result
        cik10 = re.sub(r"\D", "", cik).zfill(10)
        try:
            payload, artifact = self.get_json(
                self.source,
                SEC_SUBMISSIONS_URL.format(cik=cik10),
                cache_ttl=timedelta(hours=6),
            )
            result.artifacts.append(artifact)
        except Exception as exc:
            result.errors.append(f"SEC submissions 실패: {str(exc)[:160]}")
            return result

        form_set = {item.upper() for item in forms}
        rows_with_hash = [
            (row, artifact.data_hash)
            for row in self._rows_from_columns(dict(payload.get("filings", {}).get("recent", {})))
        ]
        if include_history_files:
            for file_info in payload.get("filings", {}).get("files", []):
                if not isinstance(file_info, dict) or not file_info.get("name"):
                    continue
                filing_to = str(file_info.get("filingTo") or "")
                if filing_to and filing_to < start_date:
                    continue
                try:
                    extra, extra_artifact = self.get_json(
                        self.source,
                        SEC_SUBMISSION_FILE_URL.format(name=file_info["name"]),
                        cache_ttl=timedelta(days=1),
                    )
                    result.artifacts.append(extra_artifact)
                    rows_with_hash.extend((row, extra_artifact.data_hash) for row in self._rows_from_columns(extra))
                except Exception as exc:
                    result.errors.append(f"SEC history file {file_info['name']} 실패: {str(exc)[:120]}")

        for row, row_raw_hash in rows_with_hash:
            form = str(row.get("form") or "").upper()
            filing_date = str(row.get("filingDate") or "")
            if form not in form_set or (filing_date and filing_date < start_date):
                continue
            event = self.normalize_filing(
                row,
                cik=cik10,
                ticker=ticker,
                company_name=company_name,
                collected_at=collected_at,
                raw_hash=row_raw_hash,
            )
            if event:
                result.events.append(event)
        result.events, processed = self.store.ingest_events(result.events, source=self.source)
        result.metadata.update(
            {
                "processed_path": str(processed or ""),
                "returned": len(result.events),
                "cik": cik10,
                "user_agent_configured": self.user_agent_configured,
            }
        )
        if not self.user_agent_configured:
            result.notes.append("SEC_USER_AGENT 미설정: 지속 수집 전 조직/연락처를 포함한 User-Agent 설정 필요")
        return result

    @staticmethod
    def _rows_from_columns(columns: dict[str, Any]) -> Iterable[dict[str, Any]]:
        arrays = {key: value for key, value in columns.items() if isinstance(value, list)}
        if not arrays:
            return []
        length = max((len(value) for value in arrays.values()), default=0)
        return [
            {key: (value[index] if index < len(value) else "") for key, value in arrays.items()}
            for index in range(length)
        ]

    @staticmethod
    def normalize_filing(
        row: dict[str, Any], *, cik: str, ticker: str, company_name: str, collected_at: str, raw_hash: str
    ) -> NormalizedEvent | None:
        accession = str(row.get("accessionNumber") or "").strip()
        form = str(row.get("form") or "").strip().upper()
        filing_date = str(row.get("filingDate") or "").strip()
        accepted_raw = str(row.get("acceptanceDateTime") or "").strip()
        primary_document = str(row.get("primaryDocument") or "").strip()
        if not accession or not form or not filing_date:
            return None
        precise = bool(accepted_raw)
        event_time = (
            iso_utc(accepted_raw, default_tz="America/New_York")
            if precise
            else iso_utc(filing_date, default_tz="America/New_York", date_at_end=True)
        )
        items = str(row.get("items") or "")
        event_type = classify_event_type(form, form=form, items=items)
        accession_path = accession.replace("-", "")
        cik_path = str(int(cik))
        source_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{accession_path}/{primary_document}"
            if primary_document
            else f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{accession_path}/"
        )
        correction = form.endswith("/A")
        title = f"SEC {form}" + (f" items {items}" if items else "")
        return NormalizedEvent(
            event_id=stable_id("SEC_EDGAR", accession, ticker, prefix="evt"),
            market="US",
            symbol=ticker.upper(),
            company_name=company_name,
            event_type=event_type,
            event_time=event_time,
            first_seen_at=event_time,
            source_type="SEC_EDGAR",
            source_name="SEC EDGAR",
            source_url=source_url,
            title=title,
            collected_at=collected_at,
            raw_reference={
                "raw_hash": raw_hash,
                "cik": cik,
                "ticker": ticker.upper(),
                "filing_type": form,
                "filing_date": filing_date,
                "acceptance_datetime_raw": accepted_raw,
                "accession_number": accession,
                "primary_document": primary_document,
                "items": items,
                "report_date": str(row.get("reportDate") or ""),
                "act": str(row.get("act") or ""),
                "file_number": str(row.get("fileNumber") or ""),
            },
            article_id=stable_id("SEC_EDGAR", accession, prefix="filing"),
            event_time_precision="second" if precise else "date",
            pit_trust="TRUSTED" if precise else "PARTIALLY_TRUSTED",
            is_correction=correction,
            entities=(company_name, ticker.upper(), cik),
            source_event_type=form,
            normalized_event_type=event_type,
            source_level=1,
            scope="COMPANY",
            relevance_status="DIRECT_COMPANY",
        )
