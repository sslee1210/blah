from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

import core.kiwoom_rest as rest_module
from core.kiwoom_rest import StockInfo
import us_ichimoku_analyzer as analyzer_module
from us_ichimoku_analyzer import USStockAnalyzer


def test_cached_current_daily_bar_is_removed_before_reuse(monkeypatch) -> None:
    eastern = ZoneInfo("America/New_York")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 3, 15, 30, tzinfo=eastern)

    monkeypatch.setattr(rest_module, "datetime", Clock)
    frame = pd.DataFrame({"close": 100.0}, index=pd.bdate_range(end="2026-09-03", periods=650))
    cache = SimpleNamespace(load_daily=lambda *args, **kwargs: frame)
    analyzer = USStockAnalyzer(SimpleNamespace(), cache)
    stock = StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술")
    actual = analyzer._daily(stock, full_history=True)
    assert actual.index[-1] == pd.Timestamp("2026-09-02")
    assert len(actual) == 649


def test_once_returns_nonzero_for_a_saved_but_partial_scan(tmp_path, monkeypatch) -> None:
    fake_analyzer = SimpleNamespace(analyze_all=lambda: ([], ["AAPL: failed"], tmp_path / "report.html", {}))
    monkeypatch.setattr(analyzer_module, "_credentials", lambda *args, **kwargs: object())
    monkeypatch.setattr(analyzer_module, "KiwoomRestClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(analyzer_module, "CacheStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(analyzer_module, "USStockAnalyzer", lambda *args, **kwargs: fake_analyzer)
    monkeypatch.setattr(analyzer_module, "single_instance", nullcontext)
    monkeypatch.setattr(analyzer_module, "REPORTS_DIR", tmp_path)
    assert analyzer_module.main(["--once", "전체 분석해줘"]) == 2


def test_scan_csv_includes_market_and_analysis_metadata(tmp_path) -> None:
    from core.ichimoku import analyze_ichimoku
    from core.reporting import AnalyzedStock
    from core.similarity import summarize_similarity
    from tests.test_ichimoku import make_frame

    frame = make_frame()
    result = AnalyzedStock(
        stock=StockInfo("AAPL", "ND", "애플", "APPLE INC", "기술"),
        daily=analyze_ichimoku(frame),
        similarity=summarize_similarity(frame),
    )
    path = tmp_path / "scan.csv"
    analyzer_module._write_scan_csv(path, [result])
    row = pd.read_csv(path).iloc[0]
    assert row["timeframe"] == "일봉"
    assert row["ichimoku_parameters"] == "9,26,52"
    assert row["data_source"] == "키움 REST API 미국주식"
    assert row["timezone"] == "America/New_York"
    assert row["currency"] == "USD"
