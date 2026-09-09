from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from news_pipeline.archive import archive_visible_items
from news_pipeline.deduplication import cluster_events, deduplicate_articles
from news_pipeline.integration import archive_analyzed_result
from news_pipeline.models import CompanyTarget, NormalizedEvent
from news_pipeline.normalization import classify_scope, gdelt_company_relevance
from news_pipeline.point_in_time import events_as_of, point_in_time_status
from news_pipeline.sources.current_news import GoogleNewsRssClient, NaverNewsClient
from news_pipeline.sources.dart import OpenDartClient
from news_pipeline.sources.edgar import SecEdgarClient
from news_pipeline.sources.gdelt import GdeltBulkClient
from news_pipeline.storage import NewsDataStore, sha256_bytes


def _event(
    event_id: str,
    *,
    title: str = "Company announces quarterly earnings",
    event_time: str = "2024-06-03T12:00:00+00:00",
    first_seen_at: str | None = "2024-06-03T12:01:00+00:00",
    url: str = "https://example.com/story",
    source: str = "TEST",
    event_type: str = "EARNINGS",
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        market="US",
        symbol="TEST",
        company_name="Test Company",
        event_type=event_type,
        event_time=event_time,
        first_seen_at=first_seen_at,
        source_type=source,
        source_name=source,
        source_url=url,
        title=title,
        collected_at="2024-06-03T12:01:00+00:00",
        article_id=f"article-{event_id}",
        pit_trust="TRUSTED",
    )


def _zip_payload(filename: str, payload: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(filename, payload.encode("utf-8"))
    return buffer.getvalue()


def test_as_of_excludes_future_event_and_future_first_seen() -> None:
    visible = _event("visible")
    future_event = _event("future-event", event_time="2024-06-03T12:02:00+00:00")
    future_seen = _event("future-seen", first_seen_at="2024-06-03T12:03:00+00:00")

    selected = events_as_of([visible, future_event, future_seen], "2024-06-03T12:01:30+00:00")

    assert [item.event_id for item in selected] == ["visible"]
    assert point_in_time_status(future_event, "2024-06-03T12:01:30+00:00")[1] == "event_after_as_of"
    assert point_in_time_status(future_seen, "2024-06-03T12:01:30+00:00")[1] == "first_seen_after_as_of"


def test_unknown_first_seen_is_excluded_from_strict_point_in_time() -> None:
    event = _event("unknown-seen", first_seen_at=None)
    assert events_as_of([event], "2024-06-04T00:00:00+00:00") == []
    assert events_as_of([event], "2024-06-04T00:00:00+00:00", require_first_seen=False) == [event]


def test_same_article_dedupes_tracking_url_and_normalized_title() -> None:
    first = _event("one", url="https://example.com/story?utm_source=a")
    second = _event("two", url="https://www.example.com/story?utm_source=b")
    third = _event("three", title="Company announces quarterly earnings!", url="https://other.example/a")

    output = deduplicate_articles([first, second, third])

    assert [item.event_id for item in output] == ["one"]


def test_same_article_can_be_associated_with_two_symbols() -> None:
    first = _event("one")
    second = NormalizedEvent.from_dict({**first.to_dict(), "event_id": "two", "symbol": "OTHER"})
    assert [item.event_id for item in deduplicate_articles([first, second])] == ["one", "two"]


def test_similar_cross_source_titles_become_event_cluster_candidates() -> None:
    first = _event("one", title="Acme wins major semiconductor supply contract", source="REUTERS")
    second = _event(
        "two",
        title="Acme wins major semiconductor supply contract from customer",
        source="OTHER",
        url="https://other.example/two",
    )

    output = cluster_events([first, second])

    assert output[0].cluster_id == output[1].cluster_id
    assert output[1].cluster_method == "title_entity_time_candidate"


def test_dart_corp_code_mapping_and_conservative_date_boundary() -> None:
    xml = """<result><list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name><stock_code>005930</stock_code><modify_date>20240101</modify_date></list></result>"""
    rows = OpenDartClient.parse_corp_codes(_zip_payload("CORPCODE.xml", xml))
    mapping = OpenDartClient.ticker_map(rows)
    event = OpenDartClient.normalize_filing(
        {
            "corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930",
            "report_nm": "[기재정정] 단일판매ㆍ공급계약체결", "rcept_no": "20240603000123",
            "rcept_dt": "20240603", "corp_cls": "Y",
        },
        symbol="005930", company_name="삼성전자",
        collected_at="2024-06-10T00:00:00+00:00", raw_hash="abc",
    )

    assert mapping["005930"]["corp_code"] == "00126380"
    assert event is not None
    assert event.event_type == "SUPPLY_CONTRACT"
    assert event.is_correction is True
    assert event.event_time == "2024-06-03T14:59:59+00:00"
    assert event.event_time_precision == "date"


def test_edgar_ticker_cik_mapping_and_acceptance_timezone() -> None:
    mapping = SecEdgarClient.ticker_map({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}})
    event = SecEdgarClient.normalize_filing(
        {
            "accessionNumber": "0000320193-24-000123", "form": "8-K", "filingDate": "2024-06-03",
            "acceptanceDateTime": "2024-06-03T21:30:00Z", "primaryDocument": "aapl-20240603.htm",
            "items": "2.02,9.01",
        },
        cik=mapping["AAPL"]["cik"], ticker="AAPL", company_name="Apple",
        collected_at="2024-06-04T00:00:00+00:00", raw_hash="def",
    )

    assert mapping["AAPL"]["cik"] == "0000320193"
    assert event is not None
    assert event.event_time == "2024-06-03T21:30:00+00:00"
    assert event.event_type == "EARNINGS"
    assert event.first_seen_at == event.event_time
    assert event.source_level == 1
    assert event.scope == "COMPANY"
    assert event.source_event_type == "8-K"
    assert event.normalized_event_type == "EARNINGS"


def test_edgar_acceptance_datetime_is_the_point_in_time_cutoff() -> None:
    event = SecEdgarClient.normalize_filing(
        {
            "accessionNumber": "0000320193-24-000123", "form": "8-K",
            "filingDate": "2024-06-03", "acceptanceDateTime": "2024-06-03T21:30:00Z",
            "primaryDocument": "aapl-20240603.htm", "items": "2.02",
        },
        cik="0000320193", ticker="AAPL", company_name="Apple Inc.",
        collected_at="2024-06-04T00:00:00+00:00", raw_hash="def",
    )
    assert event is not None
    assert events_as_of([event], "2024-06-03T21:29:59+00:00") == []
    assert events_as_of([event], "2024-06-03T21:30:00+00:00") == [event]


def test_dart_date_only_event_is_not_visible_intraday() -> None:
    event = OpenDartClient.normalize_filing(
        {
            "corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930",
            "report_nm": "단일판매ㆍ공급계약체결", "rcept_no": "20240603000123",
            "rcept_dt": "20240603", "corp_cls": "Y",
        },
        symbol="005930", company_name="삼성전자",
        collected_at="2024-06-10T00:00:00+00:00", raw_hash="abc",
    )
    assert event is not None
    assert events_as_of([event], "2024-06-03T06:00:00+00:00") == []
    assert events_as_of([event], "2024-06-03T14:59:59+00:00") == [event]
    assert event.source_level == 1
    assert event.source_event_type == "단일판매ㆍ공급계약체결"


def test_gdelt_gkg_matches_company_and_preserves_metadata() -> None:
    fields = [""] * 27
    fields[0] = "20240603120000-1"
    fields[1] = "20240603115900"
    fields[2] = "1"
    fields[3] = "example.com"
    fields[4] = "https://example.com/apple-results"
    fields[7] = "ECON_EARNINGSREPORT;TAX_FNCACT"
    fields[9] = "1#California#US#USCA#0#0#0"
    fields[11] = "tim cook"
    fields[13] = "apple inc;microsoft"
    fields[14] = "apple inc,42"
    fields[15] = "-1.2,2,3,5,10,20,100"
    payload = _zip_payload("sample.gkg.csv", "\t".join(fields) + "\n")

    events = GdeltBulkClient.parse_gkg(
        payload,
        targets=[CompanyTarget("US", "AAPL", "Apple", ("Apple Inc",))],
        collected_at="2026-01-01T00:00:00+00:00",
        raw_hash="hash",
    )

    assert len(events) == 1
    assert events[0].symbol == "AAPL"
    assert events[0].first_seen_at == "2024-06-03T12:00:00+00:00"
    assert "Apple Inc" in events[0].raw_reference["matched_aliases"]
    assert events[0].raw_reference["article_title_unavailable"] is True
    assert events[0].title == ""
    assert events[0].source_level == 3
    assert events[0].scope == "COMPANY"
    assert events[0].relevance_status == "DIRECT_COMPANY"


@pytest.mark.parametrize(
    ("organization", "target"),
    [
        ("apple", CompanyTarget("US", "AAPL", "Apple", ("Apple Inc",))),
        ("microsoft office", CompanyTarget("US", "MSFT", "Microsoft", ("Microsoft Corporation",))),
        ("samsung sdi", CompanyTarget("KR", "005930", "삼성전자", ("Samsung Electronics",))),
    ],
)
def test_gdelt_rejects_generic_product_and_affiliate_false_positives(
    organization: str, target: CompanyTarget
) -> None:
    result = gdelt_company_relevance(
        [organization], company_name=target.company_name,
        symbol=target.symbol, aliases=target.aliases,
    )
    assert result["accepted"] is False
    assert result["status"] == "REJECTED_GENERIC"


def test_company_legal_alias_is_accepted_without_ticker_matching() -> None:
    accepted = gdelt_company_relevance(
        ["Apple Inc"], company_name="Apple", symbol="AAPL", aliases=("Apple Inc",)
    )
    ticker_only = gdelt_company_relevance(
        ["AAPL"], company_name="Apple", symbol="AAPL", aliases=("Apple Inc",)
    )
    assert accepted["accepted"] is True
    assert accepted["matches"] == ("Apple Inc",)
    assert ticker_only["accepted"] is False
    samsung = gdelt_company_relevance(
        ["Samsung Electronics Co"], company_name="삼성전자", symbol="005930",
        aliases=("Samsung Electronics",),
    )
    assert samsung["accepted"] is True


def test_same_numbered_supply_event_clusters_but_distinct_amounts_stay_separate() -> None:
    first = NormalizedEvent.from_dict({
        **_event("one", title="삼성전자 5조원 반도체 공급계약", event_type="SUPPLY_CONTRACT").to_dict(),
        "market": "KR", "symbol": "005930", "company_name": "삼성전자",
        "entities": ["삼성전자", "고객사 A"],
    })
    same = NormalizedEvent.from_dict({
        **_event("two", title="삼성전자 고객사 A와 반도체 공급 계약 5조원", url="https://other.example/same", event_type="SUPPLY_CONTRACT").to_dict(),
        "market": "KR", "symbol": "005930", "company_name": "삼성전자",
        "entities": ["삼성전자", "고객사 A"],
    })
    different = NormalizedEvent.from_dict({
        **_event("three", title="삼성전자 고객사 A와 반도체 공급 계약 2조원", url="https://other.example/different", event_type="SUPPLY_CONTRACT").to_dict(),
        "market": "KR", "symbol": "005930", "company_name": "삼성전자",
        "entities": ["삼성전자", "고객사 A"],
    })
    output = cluster_events([first, same, different])
    by_id = {item.event_id: item for item in output}
    assert by_id["one"].cluster_id == by_id["two"].cluster_id
    assert by_id["three"].cluster_id != by_id["one"].cluster_id


def test_scope_classification_keeps_company_market_macro_and_global_separate() -> None:
    assert classify_scope("NVIDIA quarterly earnings") == "COMPANY"
    assert classify_scope("KOSPI stock market closes higher") == "MARKET"
    assert classify_scope("Federal Reserve interest rate decision") == "MACRO"
    assert classify_scope("Taiwan earthquake disrupts chips") == "GLOBAL_EVENT"


def test_gdelt_rejects_invalid_zip() -> None:
    with pytest.raises(ValueError, match="ZIP"):
        list(GdeltBulkClient._zip_rows(b"not-a-zip"))


class _Response:
    def __init__(self, content: bytes, *, status_error: Exception | None = None) -> None:
        self.content = content
        self.headers = {"Content-Type": "application/rss+xml"}
        self.url = "https://news.google.com/rss/search?q=test"
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls = 0
        self.last_args = ()
        self.last_kwargs = {}

    def get(self, *args, **kwargs):
        self.calls += 1
        self.last_args = args
        self.last_kwargs = kwargs
        return self.response


def test_malformed_google_feed_fails_softly(tmp_path: Path) -> None:
    client = GoogleNewsRssClient(NewsDataStore(tmp_path), session=_Session(_Response(b"<rss>")))
    result = client.search(query="AAPL", market="US", symbol="AAPL", company_name="Apple")
    assert result.status == "unavailable"
    assert "malformed feed" in result.errors[0]


def test_source_unavailable_without_naver_credentials(tmp_path: Path) -> None:
    client = NaverNewsClient(NewsDataStore(tmp_path), client_id="", client_secret="")
    result = client.search(query="삼성전자", symbol="005930", company_name="삼성전자")
    assert result.status == "unavailable"
    assert result.metadata["configured"] is False


def test_naver_forward_record_preserves_both_links_query_and_raw_hash(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "items": [
                {
                    "title": "삼성전자 공급계약 체결",
                    "originallink": "https://publisher.example/samsung",
                    "link": "https://n.news.naver.com/example",
                    "pubDate": "Mon, 03 Jun 2024 12:00:00 GMT",
                    "description": "삼성전자 관련 기사",
                }
            ]
        },
        ensure_ascii=False,
    ).encode("utf-8")
    session = _Session(_Response(payload))
    client = NaverNewsClient(
        NewsDataStore(tmp_path), client_id="configured", client_secret="configured",
        session=session,
    )
    result = client.search(
        query="삼성전자", symbol="005930", company_name="삼성전자", max_records=5
    )
    event = result.events[0]
    assert event.query == "삼성전자"
    assert event.symbol == "005930" and event.company_name == "삼성전자"
    assert event.raw_reference["original_link"] == "https://publisher.example/samsung"
    assert event.raw_reference["naver_link"] == "https://n.news.naver.com/example"
    assert event.raw_reference["raw_hash"] == result.artifacts[0].data_hash
    assert event.source_level == 2 and event.scope == "COMPANY"
    assert session.last_args[0] == "https://naverapihub.apigw.ntruss.com/search/v1/news"
    headers = session.last_kwargs["headers"]
    assert headers["X-NCP-APIGW-API-KEY-ID"] == "configured"
    assert headers["X-NCP-APIGW-API-KEY"] == "configured"
    assert "X-Naver-Client-Id" not in headers
    assert "X-Naver-Client-Secret" not in headers


def test_sec_ticker_and_submissions_share_declared_request_headers(tmp_path: Path) -> None:
    user_agent = "Test Analyzer contact@example.com"
    ticker_session = _Session(
        _Response(json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}).encode())
    )
    ticker_client = SecEdgarClient(
        NewsDataStore(tmp_path / "tickers"), user_agent=user_agent, session=ticker_session
    )
    mapping, result = ticker_client.fetch_tickers()
    assert result.status == "success" and mapping["AAPL"]["cik"] == "0000320193"

    submissions_session = _Session(
        _Response(json.dumps({"filings": {"recent": {}, "files": []}}).encode())
    )
    submissions_client = SecEdgarClient(
        NewsDataStore(tmp_path / "submissions"), user_agent=user_agent, session=submissions_session
    )
    filings = submissions_client.search_filings(
        cik="0000320193", ticker="AAPL", company_name="Apple Inc.", include_history_files=False
    )
    assert filings.status == "success"

    for session in (ticker_session, submissions_session):
        headers = session.last_kwargs["headers"]
        assert headers["User-Agent"] == user_agent
        assert headers["Accept-Encoding"] == "gzip, deflate"


def test_all_credentialed_sources_are_fail_soft_when_credentials_are_missing(tmp_path: Path) -> None:
    store = NewsDataStore(tmp_path)
    assert SecEdgarClient(store, user_agent="").fetch_tickers()[1].status == "unavailable"
    assert OpenDartClient(store, api_key="").fetch_corp_codes()[1].status == "unavailable"
    assert NaverNewsClient(store, client_id="", client_secret="").search(
        query="삼성전자", symbol="005930", company_name="삼성전자"
    ).status == "unavailable"


def test_timeout_and_malformed_official_responses_fail_softly(tmp_path: Path) -> None:
    timeout_session = _Session(_Response(b"", status_error=TimeoutError("timed out")))
    sec = SecEdgarClient(NewsDataStore(tmp_path / "sec"), user_agent="test test@example.com", session=timeout_session)
    assert sec.fetch_tickers()[1].status == "unavailable"

    malformed_session = _Session(_Response(b"not-a-zip"))
    dart = OpenDartClient(NewsDataStore(tmp_path / "dart"), api_key="configured", session=malformed_session)
    assert dart.fetch_corp_codes()[1].status == "unavailable"


def test_source_api_failure_is_recorded_not_fabricated(tmp_path: Path) -> None:
    session = _Session(_Response(b"", status_error=RuntimeError("offline")))
    client = GoogleNewsRssClient(NewsDataStore(tmp_path), session=session)
    result = client.search(query="AAPL", market="US", symbol="AAPL", company_name="Apple")
    assert result.events == []
    assert result.status == "unavailable"
    assert "offline" in result.errors[0]


def test_response_cache_reuses_raw_artifact(tmp_path: Path) -> None:
    xml = b"""<rss><channel><item><title>Apple quarterly results</title><link>https://example.com/a</link><pubDate>Mon, 03 Jun 2024 12:00:00 GMT</pubDate><source>Example</source></item></channel></rss>"""
    session = _Session(_Response(xml))
    client = GoogleNewsRssClient(NewsDataStore(tmp_path), session=session)
    first = client.search(query="AAPL", market="US", symbol="AAPL", company_name="Apple")
    second = client.search(query="AAPL", market="US", symbol="AAPL", company_name="Apple")

    assert session.calls == 1
    assert first.artifacts[0].data_hash == second.artifacts[0].data_hash
    assert second.artifacts[0].from_cache is True
    assert len(second.events) == 1
    assert second.events[0].first_seen_at == first.events[0].first_seen_at
    assert len(client.store.query_events(source_type="GOOGLE_NEWS_RSS")) == 1


def test_raw_hash_is_verified_and_tampering_is_detected(tmp_path: Path) -> None:
    store = NewsDataStore(tmp_path)
    artifact = store.store_raw("test", b"evidence", suffix="json")
    assert artifact.data_hash == sha256_bytes(b"evidence")
    Path(artifact.path).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        store.read_artifact(artifact)


def test_cached_http_artifact_does_not_persist_secret_query_parameters(tmp_path: Path) -> None:
    from news_pipeline.sources.common import CachedHttpClient

    session = _Session(_Response(b"{}"))
    client = CachedHttpClient(NewsDataStore(tmp_path), session=session)
    _, artifact = client.get_bytes(
        "dart", "https://opendart.example/api/list.json",
        params={"crtfc_key": "do-not-store-me", "corp_code": "00126380"},
        suffix="json",
    )

    assert artifact.request_url == "https://opendart.example/api/list.json"
    assert "do-not-store-me" not in json.dumps(artifact.to_dict())


def test_forward_archive_records_analysis_and_raw_hash(tmp_path: Path) -> None:
    class Item:
        title = "Apple reports quarterly earnings"
        url = "https://example.com/apple"
        source = "Example"
        published_at = "2024-06-03T12:00:00+00:00"
        collected_at = "2024-06-03T12:01:00+00:00"
        raw_hash = "abc123"
        source_type = "GOOGLE_NEWS_RSS"

    store = NewsDataStore(tmp_path)
    events = archive_visible_items(
        store,
        analysis_timestamp="2024-06-03T12:02:00+00:00",
        market="US", symbol="AAPL", company_name="Apple", query="Apple AAPL",
        source="market_intelligence", items=[Item()],
    )
    files = list((tmp_path / "forward" / "analysis").rglob("*.json"))
    payload = json.loads(files[0].read_text(encoding="utf-8"))

    assert len(events) == 1
    assert payload["symbol"] == "AAPL"
    assert payload["event_news_ids"] == [events[0].event_id]
    assert payload["raw_response_hashes"] == ["abc123"]


def test_forward_capture_archives_raw_but_excludes_future_published_item(tmp_path: Path) -> None:
    class FutureItem:
        title = "Future filing"
        url = "https://example.com/future"
        source = "Example"
        published_at = "2024-06-03T12:03:00+00:00"
        collected_at = "2024-06-03T12:01:00+00:00"
        raw_hash = "future-raw"
        source_type = "GOOGLE_NEWS_RSS"

    store = NewsDataStore(tmp_path)
    visible = archive_visible_items(
        store, analysis_timestamp="2024-06-03T12:02:00+00:00",
        market="US", symbol="AAPL", company_name="Apple", query="Apple",
        source="market_intelligence", items=[FutureItem()],
    )
    payload = json.loads(next((tmp_path / "forward" / "analysis").rglob("*.json")).read_text(encoding="utf-8"))
    assert visible == []
    assert payload["event_news_ids"] == []
    assert len(store.query_events()) == 1


def test_analysis_snapshot_preserves_result_source_groups_and_full_lineage(tmp_path: Path) -> None:
    from core.market_intelligence import MarketIntelligence, NewsItem

    store = NewsDataStore(tmp_path)
    artifact = store.store_raw(
        "google", b"raw-response", collected_at="2024-06-03T12:01:00+00:00",
        suffix="xml", query="Apple AAPL",
    )
    item = NewsItem(
        title="Apple reports quarterly earnings", url="https://example.com/apple",
        source="Example", published_at="2024-06-03T12:00:00+00:00",
        collected_at="2024-06-03T12:01:00+00:00", raw_hash=artifact.data_hash,
        source_type="GOOGLE_NEWS_RSS", original_link="https://example.com/apple",
        source_level=2, scope="COMPANY", normalized_event_type="EARNINGS",
    )
    intelligence = MarketIntelligence(
        market="US", scope="Apple", score=50, label="중립",
        market_score=50, news_score=50, event_risk=15, headlines=(item,),
    )
    result = SimpleNamespace(
        stock=SimpleNamespace(symbol="AAPL", english_name="Apple", display_name="Apple"),
        intelligence=intelligence,
        technical_score=80,
        daily=SimpleNamespace(grade="A", action="관심 후보"),
        final_action="관심 후보",
    )
    path = archive_analyzed_result(
        store, result, market="US", analysis_timestamp="2024-06-03T12:02:00+00:00"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["technical_score"] == 80
    assert payload["smoke_test"] is False
    assert payload["grade"] == "A"
    assert payload["technical_action"] == payload["final_action"] == "관심 후보"
    assert len(payload["google_news_ids"]) == 1
    assert payload["layers"]["normalized_event_ids"] == payload["google_news_ids"]
    assert payload["layers"]["raw_event_hashes"] == [artifact.data_hash]
    assert payload["lineage"][0]["cluster_id"]
    assert payload["lineage"][0]["source_level"] == 2
    assert store.read_artifact(artifact) == b"raw-response"
    assert set(payload["forward_outcome_slots"]) == {"1", "5", "10", "20"}

    replayed = store.read_analysis_snapshot(payload["analysis_id"])
    assert replayed["lineage"] == payload["lineage"]
    outcome_path = store.archive_forward_outcomes(payload["analysis_id"], {1: {"return": 0.01}})
    assert json.loads(outcome_path.read_text(encoding="utf-8"))["outcomes"]["1"]["return"] == 0.01


def test_analysis_snapshot_smoke_test_marker(tmp_path: Path) -> None:
    from core.market_intelligence import MarketIntelligence

    result = SimpleNamespace(
        stock=SimpleNamespace(symbol="AAPL", english_name="Apple", display_name="Apple"),
        intelligence=MarketIntelligence(
            market="US", scope="Apple", score=50, label="중립",
            market_score=50, news_score=50, event_risk=15,
        ),
        technical_score=65,
        daily=SimpleNamespace(grade="B", action="기다림"),
        final_action="기다림",
    )
    path = archive_analyzed_result(
        NewsDataStore(tmp_path), result, market="US",
        analysis_timestamp="2024-06-03T12:02:00+00:00", smoke_test=True,
    )
    assert json.loads(path.read_text(encoding="utf-8"))["smoke_test"] is True
