from __future__ import annotations

from dataclasses import replace

import pytest

from core.kiwoom_rest import InvalidSecurityError
from core.reporting import AnalyzedStock, render_html_report, render_individual_report
from core.similarity import SimilarityResult
from tests.test_ichimoku import make_frame
from core.ichimoku import analyze_ichimoku
from core.kiwoom_rest import StockInfo
from us_ichimoku_analyzer import _command_query, _liquidity


def test_command_query_accepts_ticker_and_korean_name() -> None:
    assert _command_query("AAPL 분석해줘") == "AAPL"
    assert _command_query("애플 분석해주세요") == "애플"
    with pytest.raises(InvalidSecurityError):
        _command_query("분석해줘")


def test_report_uses_beginner_price_labels_and_probability_warning() -> None:
    reading = analyze_ichimoku(make_frame())
    reading = replace(reading, higher_timeframe="주봉도 상승 방향", market_context="SPY·QQQ 모두 상승 방향")
    result = AnalyzedStock(
        stock=StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술", False),
        daily=reading,
        similarity=SimilarityResult(
            20,
            20,
            100.0,
            3.0,
            5,
            5,
            100.0,
            0.2,
            "과거 표본은 양호",
            match_level="엄격 일치",
            candidate_count=42,
            average_up_return_pct=3.0,
            invalidation_count=2,
            invalidation_rate=10.0,
        ),
    )
    report = render_individual_report(result)
    assert "지금 할 일" in report
    assert "시나리오 무효 가격" in report
    assert "첫 저항·목표 후보" in report
    assert "표본 조건: 엄격 일치" in report
    assert "구조 지지선 선행 이탈" in report
    assert "미래 상승 확률이나 수익을 보장하지 않습니다" in report


def test_html_report_is_standalone_responsive_and_escapes_content() -> None:
    markdown = """# A&B 분석

> **지금 할 일: 기다림**

| 항목 | 가격 |
|---|---:|
| 확인 가격 | $100.00 |

- 수익을 보장하지 않습니다.
"""
    report = render_html_report(markdown, title="A&B <분석>")
    assert report.startswith("<!doctype html>")
    assert '<meta name="viewport"' in report
    assert "A&amp;B &lt;분석&gt;" in report
    assert "<table>" in report
    assert 'data-label="항목"' in report
    assert "table-layout:fixed" in report
    assert "content:attr(data-label)" in report
    assert "<strong>지금 할 일: 기다림</strong>" in report
    assert "<script" not in report


def test_liquidity_blocks_small_dollar_volume() -> None:
    reading = analyze_ichimoku(make_frame())
    reading = replace(reading, avg_trade_value_20=1_000_000, avg_volume_20=1_000_000)
    liquid, reason = _liquidity(reading)
    assert not liquid
    assert "거래대금" in reason
