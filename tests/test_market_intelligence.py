from __future__ import annotations

from core.market_intelligence import (
    MarketIntelligence,
    MarketIntelligenceService,
    MarketMetric,
    NewsItem,
    _combine_environment_score,
    _event_risk,
    _headline_scores,
)


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


def test_event_risk_increases_with_risky_headlines() -> None:
    calm = _event_risk([NewsItem("Routine market update", "", "x", risk=0.0)])
    risky = _event_risk([NewsItem("Missile attack", "", "x", risk=1.0)])
    assert risky > calm
