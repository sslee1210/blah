from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import re

import pytest

from core.kiwoom_rest import InvalidSecurityError, KiwoomRestError
from core.reporting import AnalyzedStock, render_html_report, render_individual_report, render_scan_report, usd
from core.domestic_reporting import krw, render_domestic_individual_report, render_domestic_scan_report
from core.similarity import SimilarityResult
from tests.test_ichimoku import make_frame
from core.ichimoku import analyze_ichimoku
from core.kiwoom_rest import Quote, StockInfo
from us_ichimoku_analyzer import USStockAnalyzer, _command_query, _liquidity


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
    assert "하락 경계가" in report
    assert "첫 저항가" in report
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
    assert "table-layout:auto" in report
    assert "overflow-x:auto" in report
    assert "content:attr(data-label)" in report
    assert "<strong>지금 할 일: 기다림</strong>" in report
    assert "<script" not in report


def test_scan_html_promotes_summary_and_statuses_to_visual_badges() -> None:
    markdown = """# 전체 분석

> **결론: 관심 후보 3개 · 기다릴 종목 7개 · 피할 종목 11개**
> 유동성 부족 종목은 관심 후보에서 제외했습니다.

| 종목 | 등급 | 판단 | 가격 기준 | 과거 유사 패턴 | 평균 거래대금 | 기준 일봉 · 데이터 |
|---|:---:|---|---|---|---:|---|
| 테스트 (TEST) | A+ | 관심 후보 - 지지 확인 후 판단 | 관찰 기준 $10 · 하락 경계 $9 · 첫 저항 $12 | 7/10 상승 | $1.0B | 종가 $10 · 2026-01-01 ~ 2026-09-01 |
"""
    report = render_html_report(markdown, title="전체 분석")
    assert 'class="scan-summary"' in report
    assert '<strong>3</strong><em>종목</em>' in report
    assert 'class="grade-badge grade-aplus"' in report
    assert 'class="status-badge status-good"' in report
    assert 'class="table-wrap wide"' in report
    assert 'class="price-line"' in report


def test_liquidity_blocks_small_dollar_volume() -> None:
    reading = analyze_ichimoku(make_frame())
    reading = replace(reading, avg_trade_value_20=1_000_000, avg_volume_20=1_000_000)
    liquid, reason = _liquidity(reading)
    assert not liquid
    assert "거래대금" in reason


def test_direct_ticker_resolution_uses_master_exchange_without_blank_api_call() -> None:
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술", False)

    class FakeClient:
        def stock_info(self, *args, **kwargs):
            pytest.fail("direct stock_info call should not be needed")

    class FakeCache:
        def load_master(self):
            return [stock, stock]

    analyzer = USStockAnalyzer(FakeClient(), FakeCache())
    assert analyzer.resolve_stock("AAPL 분석해줘") == stock


def test_us_master_uses_stale_cache_when_refresh_fails() -> None:
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술", False)

    class FakeClient:
        def stock_master(self, *args, **kwargs):
            raise KiwoomRestError("temporary master failure")

    class FakeCache:
        def load_master(self, *, max_age_hours=24):
            return [stock] if max_age_hours is None else None

        def save_master(self, stocks):
            pytest.fail("stale fallback must not overwrite the cache")

    analyzer = USStockAnalyzer(FakeClient(), FakeCache())
    assert analyzer.master() == [stock]


def _report_fixture() -> AnalyzedStock:
    return AnalyzedStock(
        stock=StockInfo("TEST", "ND", "테스트", "TEST", "", False),
        daily=analyze_ichimoku(make_frame()),
        similarity=SimilarityResult(0, 0, None, None, 0, 0, None, None, "분석 기간 부족"),
    )


@pytest.mark.parametrize("renderer", [render_individual_report, render_domestic_individual_report])
def test_reports_do_not_label_fallback_daily_close_as_a_current_quote(renderer) -> None:
    result = _report_fixture()
    report = renderer(result)
    assert "현재가를 조회한 값이 아닙니다" in report
    assert "최근 조회 가격" not in report
    assert "분석 기준 종가" in report
    assert "거래량·기술지표는 분석 완료 일봉 기준" in report
    assert "하루 기준으로 환산" not in report
    assert "과거 표본 내 결과: 0건 · 분석 기간 부족" in report


@pytest.mark.parametrize(
    "renderer,expected_time,currency",
    [
        (render_individual_report, "2026-09-04 10:00:00 EDT", "USD"),
        (render_domestic_individual_report, "2026-09-04 23:00:00 KST", "KRW"),
    ],
)
def test_reports_distinguish_quote_retrieval_time_from_daily_analysis(renderer, expected_time, currency) -> None:
    result = _report_fixture()
    quote = Quote("TEST", "ND", "테스트", 12345, None, None, None, None, None, None,
                  datetime(2026, 9, 4, 14, tzinfo=timezone.utc), currency=currency)
    report = renderer(replace(result, quote=quote))
    assert "최근 조회 가격" in report
    assert f"조회 시각 {expected_time}" in report
    assert "실제 체결 시각과 다를 수 있습니다" in report
    assert result.daily.data_timestamp in report
    assert "이후 현재가 변동을 반영하지 않습니다" in report


@pytest.mark.parametrize("renderer", [render_individual_report, render_domestic_individual_report])
def test_reports_do_not_invent_a_target_without_resistance(renderer) -> None:
    result = _report_fixture()
    result = replace(result, daily=replace(result.daily, first_target_price=None))
    report = renderer(result)
    assert "첫 저항가 | 확인 불가" in report
    assert "목표 가격을 제시하지 않습니다" in report


@pytest.mark.parametrize(
    "renderer,timezone_name,cells",
    [(render_scan_report, "America/New_York", 7), (render_domestic_scan_report, "Asia/Seoul", 7)],
)
def test_scan_reports_preserve_columns_and_include_data_provenance(renderer, timezone_name, cells) -> None:
    result = _report_fixture()
    result = replace(result, stock=replace(result.stock, korean_name="A|B\n<script>bad()</script>"))
    moment = datetime(2026, 9, 4, 14, tzinfo=timezone.utc)
    report = renderer([result], started_at=moment, finished_at=moment, universe_stats={}, failures=[])
    assert timezone_name in report
    assert result.daily.source_range in report
    assert "기준 일봉 · 데이터" in report
    html = render_html_report(report, title="표 테스트")
    assert "<script>" not in html
    assert "A｜B &lt;script&gt;bad()&lt;/script&gt;" in html
    rows = re.findall(r"<tbody>(.*?)</tbody>", html, flags=re.S)
    assert len(rows) == 1
    assert rows[0].count("<td ") == cells


@pytest.mark.parametrize("formatter", [usd, krw])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_price_formatters_mark_unavailable_values(formatter, value) -> None:
    assert formatter(value) == "확인 불가"


@pytest.mark.parametrize("market", ["us", "domestic"])
@pytest.mark.parametrize("action,expected", [("기다림 - 조건 확인", 0), ("관심 후보 - 지지 확인", 1)])
def test_scan_summary_and_console_follow_the_final_action(market, action, expected, tmp_path, capsys) -> None:
    from types import SimpleNamespace
    from us_ichimoku_analyzer import run_command as run_us
    from domestic_stock_analyzer import run_command as run_domestic

    result = _report_fixture()
    result = replace(result, daily=replace(
        result.daily, grade="A", hard_blocks=(), action=action,
        higher_timeframe="주봉도 상승 방향",
        market_context="SPY·QQQ 모두 상승 방향" if market == "us" else "KOSPI·KOSDAQ 모두 상승 방향",
    ))
    renderer = render_scan_report if market == "us" else render_domestic_scan_report
    moment = datetime(2026, 9, 4, 14, tzinfo=timezone.utc)
    report = renderer([result], started_at=moment, finished_at=moment, universe_stats={}, failures=[])
    assert f"관심 후보 {expected}개 · 기다릴 종목 {1 - expected}개" in report
    analyzer = SimpleNamespace(analyze_all=lambda: ([result], [], tmp_path / "report.html", {}))
    runner = run_us if market == "us" else run_domestic
    assert runner(analyzer, "전체 분석해줘")
    assert f"관심 후보 {expected}개" in capsys.readouterr().out


@pytest.mark.parametrize("renderer", [render_scan_report, render_domestic_scan_report])
def test_scan_shows_small_sample_warning_beside_its_returns(renderer) -> None:
    result = replace(_report_fixture(), similarity=SimilarityResult(
        1, 1, 100.0, 5.0, 1, 1, 100.0, 0.2, "표본 적음", expected_return_pct=5.0,
    ))
    moment = datetime(2026, 9, 4, 14, tzinfo=timezone.utc)
    report = renderer([result], started_at=moment, finished_at=moment, universe_stats={}, failures=[])
    assert "1건 중 1건 상승 · 평균 +5.00% · 표본 적음" in report
