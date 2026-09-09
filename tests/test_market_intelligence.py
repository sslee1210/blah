from __future__ import annotations

import json

import pytest

from core.market_intelligence import (
    MarketIntelligence,
    MarketIntelligenceService,
    MarketMetric,
    NewsItem,
    _combine_environment_score,
    _dedupe_news,
    _event_risk,
    _headline_scores,
    _partition_stock_news,
)
from core.reporting import _evidence_line, _intelligence_lines
from news_pipeline.storage import NewsDataStore


def test_headline_scoring_distinguishes_good_bad_and_event_risk() -> None:
    positive, positive_risk = _headline_scores("Company beats estimates on strong demand and raises outlook")
    negative, negative_risk = _headline_scores("Company cuts outlook after weak demand and lawsuit")
    _, event_risk = _headline_scores("War and missile attack threaten supply disruption")

    assert positive > 0
    assert negative < 0
    assert positive_risk == 0
    assert negative_risk >= 0
    assert event_risk >= 0.5


def test_environment_score_penalizes_high_event_risk() -> None:
    calm_score, _ = _combine_environment_score(70, 65, 20)
    risky_score, _ = _combine_environment_score(70, 65, 90)
    assert risky_score < calm_score


def test_market_intelligence_round_trip_keeps_metrics_and_news() -> None:
    original = MarketIntelligence(
        market="US",
        scope="Apple (AAPL)",
        score=64,
        label="우호적",
        market_score=62,
        news_score=70,
        event_risk=30,
        metrics=(MarketMetric("S&P 500", "S&P500", 6500.0, 1.0, 2.0, "우호", 0.5, 1.35),),
        headlines=(NewsItem("Apple beats estimates", "https://example.com/a", "Example", sentiment=1.0),),
        generated_at="2026-09-08T08:00:00+00:00",
    )

    restored = MarketIntelligence.from_dict(original.to_dict())
    assert restored == original


def test_google_news_rss_parser_works_without_network(tmp_path) -> None:
    xml = b"""<?xml version='1.0' encoding='UTF-8'?>
    <rss><channel><item>
      <title>Markets rally on strong demand - Example News</title>
      <link>https://example.com/story</link>
      <pubDate>Tue, 08 Sep 2026 08:00:00 GMT</pubDate>
      <source>Example News</source>
    </item></channel></rss>"""

    class Response:
        content = xml

        @staticmethod
        def raise_for_status() -> None:
            return None

    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        @staticmethod
        def get(*args, **kwargs):
            return Response()

    service = MarketIntelligenceService(tmp_path / "cache.json", session=Session())
    items, error = service._google_news_search(
        "markets",
        market="US",
        max_records=5,
        when="1d",
    )

    assert error is None
    assert len(items) == 1
    assert items[0].source == "Example News"
    assert items[0].sentiment > 0


def test_naver_api_hub_contract_is_used_without_changing_news_scoring(tmp_path) -> None:
    payload = {
        "items": [
            {
                "title": "삼성전자 실적 발표",
                "originallink": "https://publisher.example/article",
                "link": "https://n.news.naver.com/article",
                "pubDate": "Mon, 03 Jun 2024 12:00:00 GMT",
            }
        ]
    }

    class Response:
        content = b"{}"

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json():
            return payload

    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.url = ""
            self.request_headers: dict[str, str] = {}

        def get(self, url, **kwargs):
            self.url = url
            self.request_headers = kwargs["headers"]
            return Response()

    session = Session()
    service = MarketIntelligenceService(tmp_path / "cache.json", session=session)
    items, error = service._naver_search("삼성전자", display=1)

    assert error is None and len(items) == 1
    assert session.url == "https://naverapihub.apigw.ntruss.com/search/v1/news"
    assert "X-NCP-APIGW-API-KEY-ID" in session.request_headers
    assert "X-NCP-APIGW-API-KEY" in session.request_headers
    assert "X-Naver-Client-Id" not in session.request_headers
    assert "X-Naver-Client-Secret" not in session.request_headers


def test_event_risk_increases_with_risky_headlines() -> None:
    calm = _event_risk([NewsItem("Routine market update", "", "x", risk=0.0)])
    risky = _event_risk([NewsItem("Missile attack", "", "x", risk=1.0)])
    assert risky > calm


def test_news_deduplication_does_not_multiply_the_same_normalized_headline() -> None:
    items = [
        NewsItem("Company wins major supply contract!", "https://a.example", "A"),
        NewsItem("Company wins major supply contract", "https://b.example", "B"),
        NewsItem("Company raises annual guidance", "https://c.example", "C"),
    ]

    deduplicated = _dedupe_news(items)

    assert [item.title for item in deduplicated] == [
        "Company wins major supply contract!",
        "Company raises annual guidance",
    ]


def test_market_intelligence_external_failures_fall_back_without_fake_signal(
    tmp_path, monkeypatch
) -> None:
    service = MarketIntelligenceService(tmp_path / "cache.json", enabled=True)
    monkeypatch.setattr(service, "_market_metrics", lambda market: ([], "market failed"))
    monkeypatch.setattr(
        service,
        "_google_news_search",
        lambda *args, **kwargs: ([], "news failed"),
    )
    monkeypatch.setattr(
        service,
        "_gdelt_search",
        lambda *args, **kwargs: ([], "event failed"),
    )

    result = service.market_overview("US")

    assert result is not None
    assert result.score == 50
    assert result.label == "정보 부족"
    assert result.market_score is None
    assert result.news_score is None
    assert result.event_risk is None
    assert any("failed" in note for note in result.notes)


@pytest.mark.parametrize(
    ("market", "scope"),
    (("KR", "국내 시장"), ("US", "미국 시장")),
)
def test_cached_market_overview_needs_no_stock_symbol_or_name(
    tmp_path, monkeypatch, market, scope
) -> None:
    service = MarketIntelligenceService(tmp_path / "cache.json", enabled=True)
    service.news_store = NewsDataStore(tmp_path / "news")
    cached = MarketIntelligence(
        market=market,
        scope=scope,
        score=50,
        label="중립",
        market_score=50,
        news_score=50,
        event_risk=10,
        generated_at="2026-09-09T05:00:00+00:00",
    )
    monkeypatch.setattr(service, "_load_cache", lambda key: cached)
    monkeypatch.setattr(
        service,
        "_official_filings",
        lambda **kwargs: pytest.fail("market overview must not request stock filings"),
    )

    result = service.market_overview(market)

    assert result is cached
    captures = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "news" / "forward" / "analysis").rglob("*.json")
    ]
    assert len(captures) == 1
    assert captures[0]["record_type"] == "SOURCE_CAPTURE"
    assert captures[0]["symbol"] == "*"
    assert not any(item["record_type"] == "ANALYSIS_SNAPSHOT" for item in captures)


def test_stock_overview_keeps_real_symbol_and_company_at_stock_level(
    tmp_path, monkeypatch
) -> None:
    service = MarketIntelligenceService(tmp_path / "cache.json", enabled=True)
    base = MarketIntelligence(
        market="US", scope="미국 시장", score=50, label="중립",
        market_score=50, news_score=50, event_risk=10,
    )
    filing_calls: list[dict[str, str]] = []
    archive_calls: list[dict[str, object]] = []
    monkeypatch.setattr(service, "_load_cache", lambda key: None)
    monkeypatch.setattr(service, "_save_cache", lambda key, value: None)
    monkeypatch.setattr(service, "_google_news_search", lambda *args, **kwargs: ([], None))
    monkeypatch.setattr(
        service,
        "_official_filings",
        lambda **kwargs: (filing_calls.append(kwargs) or ([], [], [])),
    )
    monkeypatch.setattr(
        service,
        "_archive_context",
        lambda **kwargs: archive_calls.append(kwargs),
    )

    result = service.stock_overview(
        market="US", symbol="AAPL", name="Apple Inc.", base=base
    )

    assert result is not None
    assert filing_calls == [
        {"market": "US", "symbol": "AAPL", "company_name": "Apple Inc."}
    ]
    assert archive_calls[0]["symbol"] == "AAPL"
    assert archive_calls[0]["company_name"] == "Apple Inc."


def test_report_separates_official_company_and_market_evidence() -> None:
    official = NewsItem(
        "SEC 8-K items 2.02", "https://sec.example/filing", "SEC EDGAR",
        "2026-09-08T20:00:00+00:00", source_type="SEC_EDGAR", source_level=1,
        relevance_status="DIRECT_COMPANY",
    )
    company = NewsItem(
        "Apple reports results", "https://news.example/apple", "Example News",
        "2026-09-08T21:00:00+00:00", source_type="GOOGLE_NEWS_RSS", source_level=2,
        relevance_status="DIRECT_COMPANY",
    )
    global_event = NewsItem(
        "Earthquake disrupts chip supply", "https://event.example/chips", "GDELT",
        "2026-09-08T22:00:00+00:00", source_type="GDELT_DOC", source_level=3,
        scope="GLOBAL_EVENT",
    )
    intelligence = MarketIntelligence(
        market="US", scope="Apple", score=50, label="중립",
        market_score=50, news_score=50, event_risk=15,
        official_headlines=(official,), headlines=(company,), event_headlines=(global_event,),
    )

    output = "\n".join(_intelligence_lines(intelligence))

    assert "기업 공식 공시" in output
    assert "최근 기업 뉴스" in output
    assert "산업·시장·글로벌 이벤트" in output
    assert "SEC EDGAR · 2026-09-08 20:00" in output


def test_nvda_general_market_article_is_not_company_news() -> None:
    article = NewsItem(
        "Oil prices keep rising and weigh on Wall Street",
        "https://example.com/market",
        "Example",
    )

    company, contextual = _partition_stock_news(
        [article], market="US", symbol="NVDA", company_name="NVIDIA CORP", sector="Semiconductor"
    )

    assert company == []
    assert len(contextual) == 1
    assert contextual[0].scope == "MARKET"
    assert contextual[0].relevance_status != "DIRECT_COMPANY"


def test_report_scope_gate_never_promotes_market_item_to_company_section() -> None:
    market_article = NewsItem(
        "Wall Street braces for CPI",
        "https://example.com/market",
        "Example",
        scope="MARKET",
        relevance_status="UNASSESSED",
    )
    company_article = NewsItem(
        "NVIDIA reports quarterly earnings",
        "https://example.com/nvda",
        "Example",
        scope="COMPANY",
        relevance_status="DIRECT_COMPANY",
    )
    intelligence = MarketIntelligence(
        market="US",
        scope="NVIDIA",
        score=50,
        label="중립",
        market_score=50,
        news_score=50,
        event_risk=15,
        headlines=(market_article, company_article),
    )

    output = "\n".join(_intelligence_lines(intelligence))
    company_section, context_section = output.split("### 산업·시장·글로벌 이벤트", 1)

    assert "NVIDIA reports quarterly earnings" in company_section
    assert "Wall Street braces for CPI" not in company_section
    assert "Wall Street braces for CPI" in context_section


def test_dart_date_only_and_synthetic_time_hide_fake_clock_precision() -> None:
    item = NewsItem(
        "반기보고서",
        "https://dart.example/filing",
        "OpenDART",
        "2026-08-14T14:59:59+00:00",
        source_type="OPENDART",
        source_level=1,
        relevance_status="DIRECT_COMPANY",
        event_time_precision="date",
        synthetic_time=True,
    )

    line = _evidence_line(item)

    assert line.endswith("OpenDART · 2026-08-14")
    assert "14:59" not in line
