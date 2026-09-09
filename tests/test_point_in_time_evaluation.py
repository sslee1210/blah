from __future__ import annotations

import numpy as np
import pandas as pd

from core.kiwoom_rest import StockInfo
from core.ichimoku import analyze_ichimoku, apply_context
from core.market_intelligence import MarketIntelligence
from core.reporting import AnalyzedStock
from core.similarity import summarize_similarity
from us_ichimoku_analyzer import _liquidity as _us_liquidity
from evaluation.point_in_time import (
    BacktestConfig,
    _forward_outcomes,
    _finalize_trust,
    _intelligence_for,
    aggregate_performance,
    apply_walk_forward_splits,
    evaluate_point_in_time,
    ranking_performance,
)


def _frame(rows: int = 220) -> pd.DataFrame:
    index = pd.bdate_range("2020-01-02", periods=rows)
    trend = np.linspace(90.0, 150.0, rows)
    wave = np.sin(np.arange(rows) / 6.0) * 2.0
    close = trend + wave
    return pd.DataFrame(
        {
            "open": close * 0.997,
            "high": close * 1.015,
            "low": close * 0.985,
            "close": close,
            "volume": np.full(rows, 1_000_000.0),
            "trade_value": close * 1_000_000.0,
        },
        index=index,
    )


def _stock(symbol: str = "TEST") -> StockInfo:
    return StockInfo(symbol, "NY", symbol, symbol, "테스트", False)


def test_forward_outcomes_use_next_open_and_requested_horizon() -> None:
    frame = _frame(100)
    position = 80
    rows = _forward_outcomes(
        {"as_of": frame.index[position].date().isoformat(), "symbol": "TEST"},
        frame=frame,
        position=position,
        horizons=(1, 5),
        invalidation_price=float(frame.iloc[position]["close"] * 0.90),
        large_loss_threshold_pct=-10.0,
    )
    assert rows[0]["entry_price"] == frame.iloc[position + 1]["open"]
    assert rows[0]["exit_price"] == frame.iloc[position + 1]["close"]
    assert rows[1]["exit_price"] == frame.iloc[position + 5]["close"]
    assert rows[1]["mae_pct"] <= rows[1]["mfe_pct"]


def test_point_in_time_signals_do_not_change_when_later_prices_change() -> None:
    original = _frame(220)
    changed = original.copy()
    cutoff = original.index[150]
    changed.loc[changed.index > cutoff, ["open", "high", "low", "close"]] *= 3.0
    changed.loc[changed.index > cutoff, "trade_value"] *= 3.0
    config = BacktestConfig(
        market="US",
        decision_step=25,
        min_cross_section=2,
        min_split_dates=5,
        include_similarity_features=False,
    )
    proxies = {"SPY": original, "QQQ": original}
    left = evaluate_point_in_time(
        {"TEST": (_stock(), original)}, config=config, market_proxies=proxies
    )
    right = evaluate_point_in_time(
        {"TEST": (_stock(), changed)}, config=config, market_proxies=proxies
    )
    columns = [
        "as_of",
        "grade",
        "action",
        "technical_score",
        "bullish_score",
        "hard_block_count",
        "similar_sample_count",
    ]
    left_common = left.signals.loc[pd.to_datetime(left.signals["as_of"]) <= cutoff, columns]
    right_common = right.signals.loc[pd.to_datetime(right.signals["as_of"]) <= cutoff, columns]
    pd.testing.assert_frame_equal(
        left_common.reset_index(drop=True), right_common.reset_index(drop=True)
    )


def test_baseline_replay_matches_existing_production_objects() -> None:
    frame = _frame(220)
    config = BacktestConfig(
        market="US",
        decision_step=500,
        min_cross_section=2,
        min_split_dates=5,
        include_similarity_features=False,
    )
    bundle = evaluate_point_in_time(
        {"TEST": (_stock(), frame)},
        config=config,
        market_proxies={"SPY": frame, "QQQ": frame},
    )
    history = frame.iloc[:80]
    raw = analyze_ichimoku(history, timeframe="일봉")
    contextual = apply_context(
        raw,
        weekly=None,
        market_label="SPY·QQQ 모두 상승 방향",
        market_is_weak=False,
    )
    liquid, reason = _us_liquidity(contextual)
    assert liquid
    expected = AnalyzedStock(
        stock=_stock(),
        daily=contextual,
        similarity=summarize_similarity(history),
        liquid=True,
        liquidity_reason=reason,
    )
    actual = bundle.signals.iloc[0]
    assert actual["grade"] == expected.daily.grade
    assert actual["action_text"] == expected.final_action
    assert actual["technical_score"] == expected.technical_score
    assert actual["context_adjusted_score"] == expected.context_adjusted_score


def test_news_snapshot_after_market_close_is_not_allowed_into_same_day_signal() -> None:
    base = dict(
        market="US",
        scope="market",
        score=50,
        label="중립",
        market_score=50,
        news_score=50,
        event_risk=15,
    )
    before = MarketIntelligence(**base, generated_at="2024-06-03T15:59:00-04:00")
    after = MarketIntelligence(**base, generated_at="2024-06-03T16:01:00-04:00")
    as_of = pd.Timestamp("2024-06-03")
    assert _intelligence_for({("2024-06-03", "*"): before}, as_of, "TEST") is before
    assert _intelligence_for({("2024-06-03", "*"): after}, as_of, "TEST") is None


def test_empty_inputs_are_reported_not_evaluable_without_fake_metrics() -> None:
    bundle = evaluate_point_in_time({}, config=BacktestConfig(market="KR"))
    assert bundle.metadata["status"] == "not_evaluable"
    assert bundle.metadata["trusted_baseline_available"] is False
    assert (bundle.baseline["sample_count"] == 0).all()
    assert (bundle.rank_ic["date_count"] == 0).all()
    assert set(bundle.ablation["family"]) == {
        "일목 구조",
        "ADX / DI",
        "ATR",
        "거래량",
        "거래대금",
        "주봉 확인",
        "60분봉",
        "과거 유사패턴",
        "시장 방향",
        "시장·뉴스 점수",
        "뉴스",
        "글로벌 이벤트",
    }


def test_aggregate_performance_calculates_requested_metrics() -> None:
    frame = pd.DataFrame(
        {
            "as_of": ["2024-01-01", "2024-01-02"],
            "horizon": [5, 5],
            "grade": ["A", "A"],
            "forward_return_pct": [10.0, -5.0],
            "mae_pct": [-2.0, -8.0],
            "mfe_pct": [12.0, 3.0],
            "max_drawdown_pct": [-3.0, -9.0],
            "invalidation_hit": [False, True],
            "large_loss": [False, False],
        }
    )
    result = aggregate_performance(frame, ["grade"]).iloc[0]
    assert result["sample_count"] == 2
    assert result["mean_return_pct"] == 2.5
    assert result["median_return_pct"] == 2.5
    assert result["up_rate_pct"] == 50.0
    assert result["invalidation_rate_pct"] == 50.0


def test_walk_forward_has_purges_and_keeps_test_out_of_selection() -> None:
    dates = [item.date().isoformat() for item in pd.bdate_range("2020-01-01", periods=50)]
    signals = pd.DataFrame({"as_of": dates})
    outcomes = pd.DataFrame(
        {
            "as_of": dates,
            "horizon": 5,
            "action": "기다림",
            "forward_return_pct": 0.0,
            "mae_pct": 0.0,
            "mfe_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "invalidation_hit": False,
            "large_loss": False,
        }
    )
    split_signals, split_outcomes, metadata = apply_walk_forward_splits(
        signals, outcomes, purge_sessions=2, min_split_dates=10
    )
    assert metadata["status"] == "applied"
    assert metadata["test_used_for_selection"] is False
    assert {"train", "validation", "test", "purged"}.issubset(
        set(split_signals["split"])
    )
    assert set(split_signals["split"]) == set(split_outcomes["split"])


def test_ranking_requires_cross_section_and_reports_top_bottom_groups() -> None:
    rows: list[dict[str, object]] = []
    for horizon in (5, 10, 20):
        for day in ("2024-01-02", "2024-01-03"):
            for index in range(20):
                rows.append(
                    {
                        "as_of": day,
                        "symbol": f"S{index:02d}",
                        "horizon": horizon,
                        "technical_score": index,
                        "context_adjusted_score": index,
                        "forward_return_pct": float(index),
                        "mae_pct": -1.0,
                        "mfe_pct": 2.0,
                        "max_drawdown_pct": -1.0,
                        "invalidation_hit": False,
                        "large_loss": False,
                    }
                )
    ic, groups = ranking_performance(pd.DataFrame(rows), min_cross_section=20)
    assert (ic["mean_spearman"] == 1.0).all()
    assert {"Top 5%", "Top 10%", "Top 20%", "Bottom 20%"}.issubset(
        set(groups["rank_group"])
    )


def test_technical_baseline_data_gate_does_not_require_news_snapshot() -> None:
    frame = _frame(220)
    days = {
        item.date().isoformat(): {"TEST"}
        for item in frame.index
    }
    bundle = evaluate_point_in_time(
        {"TEST": (_stock(), frame)},
        config=BacktestConfig(
            market="US",
            baseline_mode="TECHNICAL_BASELINE",
            dataset_point_in_time_verified=True,
            price_policy_verified=True,
            decision_step=50,
            min_split_dates=5,
            include_similarity_features=False,
        ),
        market_proxies={"SPY": frame, "QQQ": frame},
        universe_snapshots=days,
    )
    assert bundle.metadata["data_requirements"]["intelligence_complete"] is True
    assert bundle.metadata["intelligence_snapshot_count"] == 0


def test_full_context_baseline_still_requires_historical_intelligence() -> None:
    frame = _frame(220)
    days = {item.date().isoformat(): {"TEST"} for item in frame.index}
    bundle = evaluate_point_in_time(
        {"TEST": (_stock(), frame)},
        config=BacktestConfig(
            market="US",
            baseline_mode="FULL_CONTEXT_BASELINE",
            dataset_point_in_time_verified=True,
            price_policy_verified=True,
            decision_step=50,
            min_split_dates=5,
            include_similarity_features=False,
        ),
        market_proxies={"SPY": frame, "QQQ": frame},
        universe_snapshots=days,
    )
    assert bundle.metadata["trusted_baseline_available"] is False
    assert any("FULL_CONTEXT_BASELINE" in item for item in bundle.metadata["limitations"])


def test_adjusted_outcome_does_not_change_raw_invalidation_comparison() -> None:
    raw = _frame(100)
    adjusted = raw.copy()
    adjusted.loc[:, ["open", "high", "low", "close"]] /= 2.0
    position = 80
    invalidation = float(raw.iloc[position + 1]["low"] + 0.01)
    rows = _forward_outcomes(
        {"as_of": raw.index[position].date().isoformat(), "symbol": "TEST"},
        frame=adjusted,
        signal_frame=raw,
        position=position,
        horizons=(5,),
        invalidation_price=invalidation,
        large_loss_threshold_pct=-10.0,
    )
    assert rows[0]["invalidation_hit"] is True
    assert rows[0]["entry_price"] == adjusted.iloc[position + 1]["open"]


def test_trusted_requires_delisted_calendar_and_enough_independent_dates() -> None:
    frame = _frame(300)
    days = {item.date().isoformat(): {"TEST"} for item in frame.index}
    bundle = evaluate_point_in_time(
        {"TEST": (_stock(), frame)},
        config=BacktestConfig(
            market="US",
            baseline_mode="TECHNICAL_BASELINE",
            dataset_point_in_time_verified=True,
            price_policy_verified=True,
            delisted_securities_included=True,
            exchange_calendar_verified=True,
            decision_step=3,
            min_split_dates=5,
            include_similarity_features=False,
        ),
        market_proxies={"SPY": frame, "QQQ": frame},
        universe_snapshots=days,
    )
    assert bundle.metadata["walk_forward"]["status"] == "applied"
    assert bundle.metadata["trust_level"] == "TRUSTED"
    assert bundle.metadata["trusted_baseline_available"] is True


def test_trust_levels_do_not_promote_current_survivors_or_short_periods() -> None:
    requirements = {
        "price_history": True,
        "market_proxies": True,
        "universe_snapshots": True,
        "historical_universe_verified": False,
        "delisted_securities_included": False,
        "corporate_action_verified": False,
        "exchange_calendar_verified": True,
        "intelligence_complete": True,
        "independent_dates_sufficient": False,
    }
    exploratory = {"status": "evaluated", "data_requirements": requirements}
    _finalize_trust(exploratory, {"status": "applied"})
    assert exploratory["trust_level"] == "EXPLORATORY"
    assert exploratory["trusted_baseline_available"] is False

    almost = {"status": "evaluated", "data_requirements": {key: True for key in requirements}}
    _finalize_trust(almost, {"status": "insufficient_dates"})
    assert almost["trust_level"] == "PARTIALLY_TRUSTED"
    assert "independent_dates_sufficient" in almost["trust_blockers"]
