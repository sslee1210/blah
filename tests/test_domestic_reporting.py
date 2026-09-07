from __future__ import annotations

from dataclasses import replace

from core.domestic_reporting import render_domestic_individual_report
from core.ichimoku import analyze_ichimoku
from core.kiwoom_rest import StockInfo
from core.reporting import AnalyzedStock
from core.similarity import SimilarityResult
from tests.test_ichimoku import make_frame


def test_domestic_report_uses_krw_and_domestic_source_without_us_labels() -> None:
    reading = analyze_ichimoku(make_frame())
    reading = replace(
        reading,
        higher_timeframe="주봉도 상승 방향",
        market_context="KOSPI·KOSDAQ 모두 상승 방향",
    )
    result = AnalyzedStock(
        stock=StockInfo("005930", "KOSPI", "삼성전자", "", "전기전자", False),
        daily=reading,
        similarity=SimilarityResult(0, 0, None, None, 0, 0, None, None, "표본 부족"),
    )
    report = render_domestic_individual_report(result)
    assert "국내주식 분석" in report
    assert "₩" in report
    assert "키움 REST API 국내주식" in report
    assert "가격 통화: KRW" in report
    assert "USD" not in report
    assert "SPY" not in report
    assert "FOMC" not in report
