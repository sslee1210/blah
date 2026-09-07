from __future__ import annotations

from dataclasses import replace
from contextlib import nullcontext

import pytest

from core.ichimoku import analyze_ichimoku, resample_weekly
from core.kiwoom_rest import KiwoomRestError, StockInfo
from domestic_stock_analyzer import (
    DomesticStockAnalyzer,
    IncompleteDomesticScanError,
    _apply_domestic_context,
    _command_query,
    run_command,
)
from tests.test_ichimoku import make_frame


class _FakeCache:
    def __init__(self, stocks):
        self.stocks = stocks

    def load_master(self):
        return self.stocks


class _FakeClient:
    pass


def test_domestic_command_query_accepts_name_numeric_and_alpha_code() -> None:
    assert _command_query("삼성전자 분석해줘") == "삼성전자"
    assert _command_query("005930 분석해줘") == "005930"
    assert _command_query("0001A0 국내주식 분석해줘") == "0001A0"


def test_domestic_resolve_stock_keeps_leading_zero_and_name_lookup() -> None:
    samsung = StockInfo("005930", "KOSPI", "삼성전자", "", "전기전자", False)
    analyzer = DomesticStockAnalyzer(_FakeClient(), _FakeCache([samsung]))
    assert analyzer.resolve_stock("005930 분석해줘") == samsung
    assert analyzer.resolve_stock("삼성전자 분석해줘") == samsung


def test_domestic_context_contains_no_us_proxy_text_and_mixed_is_only_risk() -> None:
    daily = analyze_ichimoku(make_frame())
    weekly = analyze_ichimoku(resample_weekly(make_frame()), timeframe="주봉")
    contextual = _apply_domestic_context(
        daily,
        weekly=weekly,
        market_label="KOSPI·KOSDAQ 방향이 엇갈림",
        market_is_weak=False,
    )
    assert "SPY" not in " ".join((*contextual.hard_blocks, *contextual.risks))
    assert any("KOSPI" in item for item in contextual.risks)
    assert not any("KOSPI" in item for item in contextual.hard_blocks)
    assert contextual.action.startswith("기다림")


def test_domestic_weak_market_is_hard_block() -> None:
    daily = analyze_ichimoku(make_frame())
    contextual = _apply_domestic_context(
        daily,
        weekly=None,
        market_label="KOSPI·KOSDAQ 모두 약세",
        market_is_weak=True,
    )
    assert any("KOSPI" in item for item in contextual.hard_blocks)


def test_domestic_unavailable_market_is_hard_block() -> None:
    daily = analyze_ichimoku(make_frame())
    contextual = _apply_domestic_context(
        daily,
        weekly=None,
        market_label="KOSPI·KOSDAQ 일부 확인 불가",
        market_is_weak=False,
    )
    assert any("확인하지 못했습니다" in item for item in contextual.hard_blocks)
    assert contextual.action.startswith("기다림")


def test_domestic_master_uses_stale_cache_when_refresh_fails() -> None:
    samsung = StockInfo("005930", "KOSPI", "삼성전자", "", "전기전자", False)

    class FakeClient:
        def stock_master(self):
            raise KiwoomRestError("temporary master failure")

    class FakeCache:
        def load_master(self, *, max_age_hours=24):
            return [samsung] if max_age_hours is None else None

        def save_master(self, stocks):
            raise AssertionError("stale fallback must not overwrite the cache")

    analyzer = DomesticStockAnalyzer(FakeClient(), FakeCache())
    assert analyzer.master() == [samsung]


def test_domestic_missing_weekly_confirmation_blocks_interest_and_updates_risk() -> None:
    daily = replace(
        analyze_ichimoku(make_frame()),
        grade="A+", confidence="높음", action="관심 후보", hard_blocks=(), risks=(), risk_score=0,
    )
    contextual = _apply_domestic_context(
        daily, weekly=None, market_label="KOSPI·KOSDAQ 모두 상승 방향", market_is_weak=False
    )
    assert contextual.higher_timeframe == "확인 불가"
    assert any("주봉" in item for item in contextual.hard_blocks)
    assert contextual.action.startswith("기다림")
    assert contextual.confidence != "높음"
    assert contextual.risk_score == len(contextual.risks) + 2 * len(contextual.hard_blocks)


def test_daily_reuses_current_cache_for_a_stock_with_less_than_two_years_of_history() -> None:
    frame = make_frame(rows=200)
    stock = StockInfo("005930", "KOSPI", "삼성전자", "", "전기전자", False)

    class FakeCache:
        def load_daily(self, requested, *, fresh_only):
            assert requested == stock and fresh_only
            return frame

    class FakeClient:
        def daily_bars(self, *args, **kwargs):
            raise AssertionError("a current, analyzable cache does not need another API request")

    analyzer = DomesticStockAnalyzer(FakeClient(), FakeCache())
    assert analyzer._daily(stock).equals(frame)


def test_partial_scan_prints_report_and_signals_failure(tmp_path, capsys) -> None:
    report = tmp_path / "report.html"

    class FakeAnalyzer:
        def analyze_all(self):
            return [], ["005930: failed"], report, {"selected": 1}

    with pytest.raises(IncompleteDomesticScanError):
        run_command(FakeAnalyzer(), "국내 전체 분석해줘")
    assert str(report) in capsys.readouterr().out


def test_once_returns_two_on_partial_scan(monkeypatch, tmp_path) -> None:
    import domestic_stock_analyzer as app

    class FakeAnalyzer:
        def __init__(self, *args):
            pass

        def analyze_all(self):
            return [], ["005930: failed"], tmp_path / "report.html", {"selected": 1}

    monkeypatch.setattr(app, "_credentials", lambda *args, **kwargs: object())
    monkeypatch.setattr(app, "DomesticKiwoomRestClient", lambda *args: object())
    monkeypatch.setattr(app, "DomesticCacheStore", lambda *args: object())
    monkeypatch.setattr(app, "DomesticStockAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(app, "single_instance", nullcontext)
    monkeypatch.setattr(app, "REPORTS_DIR", tmp_path)
    assert app.main(["--once", "국내 전체 분석해줘"]) == 2


def test_individual_console_can_display_an_unknown_first_target(tmp_path, capsys) -> None:
    from core.reporting import AnalyzedStock
    from core.similarity import SimilarityResult

    reading = replace(analyze_ichimoku(make_frame()), first_target_price=None)
    result = AnalyzedStock(
        stock=StockInfo("005930", "KOSPI", "삼성전자", "", "전기전자", False),
        daily=reading,
        similarity=SimilarityResult(0, 0, None, None, 0, 0, None, None, "표본 부족"),
    )

    class FakeAnalyzer:
        def analyze_individual(self, text):
            return result, tmp_path / "report.html"

    assert run_command(FakeAnalyzer(), "삼성전자 분석해줘")
    assert "첫 목표 후보 확인 불가" in capsys.readouterr().out
