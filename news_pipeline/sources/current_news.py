from __future__ import annotations

"""Current-search collectors.  They become PIT evidence only after collection."""

from datetime import timedelta
from html import unescape
import os
import re
from xml.etree import ElementTree

import requests

from ..models import CollectionResult, NormalizedEvent
from ..normalization import (
    canonical_url,
    classify_event_type,
    company_news_relevance,
    iso_utc,
    stable_id,
)
from ..storage import NewsDataStore
from .common import CachedHttpClient, utc_now


NAVER_NEWS_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", str(value or "")))).strip()


class NaverNewsClient(CachedHttpClient):
    source = "naver"

    def __init__(
        self,
        store: NewsDataStore,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(store, session=session, timeout=timeout)
        self.client_id = client_id if client_id is not None else os.getenv("NAVER_CLIENT_ID", "")
        self.client_secret = client_secret if client_secret is not None else os.getenv("NAVER_CLIENT_SECRET", "")

    @property
    def available(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def search(self, *, query: str, symbol: str, company_name: str, max_records: int = 100) -> CollectionResult:
        collected_at = utc_now()
        result = CollectionResult(source=self.source, collected_at=collected_at)
        if not self.available:
            result.errors.append("NAVER_CLIENT_ID/NAVER_CLIENT_SECRET 미설정")
            result.metadata["configured"] = False
            return result
        params = {"query": query, "display": max(1, min(100, max_records)), "start": 1, "sort": "date"}
        try:
            payload, artifact = self.get_json(
                self.source,
                NAVER_NEWS_URL,
                params=params,
                headers={
                    "X-NCP-APIGW-API-KEY-ID": self.client_id,
                    "X-NCP-APIGW-API-KEY": self.client_secret,
                },
                query=query,
                cache_ttl=timedelta(minutes=10),
            )
            result.artifacts.append(artifact)
        except Exception as exc:
            result.errors.append(f"Naver News Search API 실패: {str(exc)[:160]}")
            return result
        for row in payload.get("items", []):
            if not isinstance(row, dict):
                continue
            title = _clean(str(row.get("title", "")))
            original_link = str(row.get("originallink") or "")
            naver_link = str(row.get("link") or "")
            if not title:
                continue
            try:
                event_time = iso_utc(str(row.get("pubDate", "")))
            except (TypeError, ValueError):
                continue
            source_url = original_link or naver_link
            article_id = stable_id(canonical_url(source_url), title, prefix="article")
            relevance = company_news_relevance(
                title, company_name=company_name, symbol=symbol
            )
            result.events.append(
                NormalizedEvent(
                    event_id=stable_id("NAVER_NEWS", article_id, symbol, prefix="evt"),
                    market="KR",
                    symbol=symbol.upper(),
                    company_name=company_name,
                    event_type=classify_event_type(title),
                    event_time=event_time,
                    first_seen_at=collected_at,
                    source_type="NAVER_NEWS",
                    source_name="Naver News Search API",
                    source_url=source_url,
                    title=title,
                    collected_at=collected_at,
                    raw_reference={
                        "raw_hash": artifact.data_hash,
                        "original_link": original_link,
                        "naver_link": naver_link,
                        "published_at_raw": str(row.get("pubDate", "")),
                        "description": _clean(str(row.get("description", ""))),
                    },
                    query=query,
                    article_id=article_id,
                    event_time_precision="second",
                    pit_trust="TRUSTED",
                    source_event_type="NEWS_ARTICLE",
                    normalized_event_type=classify_event_type(title),
                    source_level=2,
                    scope=str(relevance["scope"]),
                    relevance_status=str(relevance["status"]),
                    entities=tuple(str(value) for value in relevance["matches"]),
                )
            )
        result.events, processed = self.store.ingest_events(result.events, source=self.source)
        result.metadata.update({"configured": True, "processed_path": str(processed or ""), "returned": len(result.events)})
        return result


class GoogleNewsRssClient(CachedHttpClient):
    source = "google"

    def search(
        self,
        *,
        query: str,
        market: str,
        symbol: str,
        company_name: str,
        when: str = "3d",
        max_records: int = 50,
    ) -> CollectionResult:
        collected_at = utc_now()
        result = CollectionResult(source=self.source, collected_at=collected_at)
        is_kr = market.upper() == "KR"
        lang, country = ("ko", "KR") if is_kr else ("en", "US")
        params = {"q": f"{query} when:{when}", "hl": f"{lang}-{country}", "gl": country, "ceid": f"{country}:{lang}"}
        try:
            payload, artifact = self.get_bytes(
                self.source,
                GOOGLE_NEWS_RSS_URL,
                params=params,
                suffix="xml",
                content_type="application/rss+xml",
                query=query,
                cache_ttl=timedelta(minutes=10),
            )
            result.artifacts.append(artifact)
            root = ElementTree.fromstring(payload)
        except Exception as exc:
            result.errors.append(f"Google News RSS 실패 또는 malformed feed: {str(exc)[:160]}")
            return result
        for node in root.findall(".//item")[: max(1, min(100, max_records))]:
            title = _clean(node.findtext("title", default=""))
            link = node.findtext("link", default="").strip()
            pub_date = node.findtext("pubDate", default="")
            source_node = node.find("source")
            source_name = _clean(source_node.text or "") if source_node is not None else "Google News"
            if not title:
                continue
            try:
                event_time = iso_utc(pub_date)
            except (TypeError, ValueError):
                continue
            article_id = stable_id(canonical_url(link), title, prefix="article")
            relevance = company_news_relevance(
                title, company_name=company_name, symbol=symbol
            )
            result.events.append(
                NormalizedEvent(
                    event_id=stable_id("GOOGLE_NEWS_RSS", article_id, symbol, prefix="evt"),
                    market=market.upper(),
                    symbol=symbol.upper(),
                    company_name=company_name,
                    event_type=classify_event_type(title),
                    event_time=event_time,
                    first_seen_at=collected_at,
                    source_type="GOOGLE_NEWS_RSS",
                    source_name=source_name or "Google News",
                    source_url=link,
                    title=title,
                    collected_at=collected_at,
                    raw_reference={"raw_hash": artifact.data_hash, "published_at_raw": pub_date},
                    query=query,
                    article_id=article_id,
                    event_time_precision="second",
                    pit_trust="PARTIALLY_TRUSTED",
                    source_event_type="NEWS_ARTICLE",
                    normalized_event_type=classify_event_type(title),
                    source_level=2,
                    scope=str(relevance["scope"]),
                    relevance_status=str(relevance["status"]),
                    entities=tuple(str(value) for value in relevance["matches"]),
                )
            )
        result.events, processed = self.store.ingest_events(result.events, source=self.source)
        result.metadata.update(
            {
                "processed_path": str(processed or ""),
                "returned": len(result.events),
                "historical_source": False,
                "coverage_note": "비공식 검색 RSS endpoint의 현재 snapshot이며 완전한 역사 archive가 아님",
            }
        )
        return result
