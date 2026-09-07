from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.ichimoku import analyze_ichimoku, apply_context, calculate_ichimoku, resample_weekly


def make_frame(rows: int = 520, *, trend: float = 0.12) -> pd.DataFrame:
    index = pd.bdate_range("2022-01-03", periods=rows)
    base = 50.0 + np.arange(rows) * trend + np.sin(np.arange(rows) / 7.0)
    frame = pd.DataFrame(
        {
            "open": base - 0.15,
            "high": base + 0.8,
            "low": base - 0.8,
            "close": base,
            "volume": 1_000_000 + (np.arange(rows) % 20) * 10_000,
        },
        index=index,
    )
    frame["trade_value"] = frame["close"] * frame["volume"]
    return frame


def test_cloud_at_time_t_does_not_use_future_rows() -> None:
    frame = make_frame()
    cut = 240
    full = calculate_ichimoku(frame)
    truncated = calculate_ichimoku(frame.iloc[: cut + 1])
    for column in ("tenkan", "kijun", "cloud_a", "cloud_b", "cloud_top", "cloud_bottom"):
        assert full.iloc[cut][column] == truncated.iloc[-1][column]


def test_bullish_series_has_positive_structure() -> None:
    reading = analyze_ichimoku(make_frame())
    assert reading.price_position == "구름 위"
    assert reading.tenkan >= reading.kijun
    assert reading.grade in {"A+", "A"}
    assert reading.invalidation_price < reading.close < reading.first_target_price
    assert reading.atr14 > 0
    assert reading.atr14_pct > 0
    assert reading.adx14 >= 0
    assert reading.plus_di14 >= 0
    assert reading.minus_di14 >= 0
    assert reading.reward_risk_ratio >= 0
    assert reading.risk_score == len(reading.risks) + 2 * len(reading.hard_blocks)
    assert abs((reading.close - reading.invalidation_price) - max(0.01, reading.close - reading.invalidation_price)) < 1e-9


def test_weekly_resampling_preserves_ohlcv_meaning() -> None:
    weekly = resample_weekly(make_frame())
    assert len(weekly) >= 100
    assert (weekly["high"] >= weekly[["open", "close"]].max(axis=1)).all()
    assert (weekly["low"] <= weekly[["open", "close"]].min(axis=1)).all()
    assert (weekly["volume"] > 0).all()


def test_weekly_resampling_excludes_unfinished_current_week() -> None:
    frame = make_frame().loc[:"2023-01-05"]  # Thursday: Friday bucket is not complete yet.
    weekly = resample_weekly(frame)
    assert weekly.index[-1].date() <= frame.index[-1].date()
    assert weekly.index[-1].weekday() == 4


def test_unconfirmed_market_blocks_interest_even_for_a_plus() -> None:
    daily = analyze_ichimoku(make_frame())
    weekly = analyze_ichimoku(resample_weekly(make_frame()), timeframe="주봉")
    contextual = apply_context(
        daily,
        weekly=weekly,
        market_label="SPY·QQQ 방향이 엇갈림",
        market_is_weak=False,
    )
    assert any("SPY" in reason for reason in contextual.hard_blocks)
    assert contextual.action.startswith("기다림")


@pytest.mark.parametrize("spread", [0.0, 1.0])
def test_unchanging_prices_have_zero_adx_instead_of_failing(spread: float) -> None:
    frame = make_frame()
    frame[["open", "close"]] = 50.0
    frame["high"] = 50.0 + spread
    frame["low"] = 50.0 - spread
    reading = analyze_ichimoku(frame)
    assert reading.adx14 == 0.0
    assert reading.plus_di14 == reading.minus_di14 == 0.0
    assert not reading.action.startswith("관심")


@pytest.mark.parametrize(
    ("column", "value"),
    [("high", 1.0), ("low", 999.0), ("close", np.inf), ("open", np.nan),
     ("volume", -1.0), ("volume", np.inf), ("trade_value", -1.0)],
)
def test_corrupt_bar_is_rejected_without_silently_changing_periods(column, value) -> None:
    frame = make_frame()
    frame[column] = frame[column].astype(float)
    frame.loc[frame.index[-1], column] = value
    with pytest.raises(ValueError):
        analyze_ichimoku(frame)


def test_invalid_timestamps_are_rejected() -> None:
    frame = make_frame()
    frame.index = pd.RangeIndex(len(frame))
    with pytest.raises(ValueError, match="날짜"):
        analyze_ichimoku(frame)


def test_missing_weekly_data_blocks_interest_and_context_score_matches_details() -> None:
    from dataclasses import replace

    daily = replace(
        analyze_ichimoku(make_frame()), grade="A+", confidence="높음",
        hard_blocks=(), risks=(), risk_score=0, action="관심 후보",
    )
    contextual = apply_context(
        daily, weekly=None, market_label="SPY·QQQ 모두 상승 방향", market_is_weak=False,
    )
    assert any("주봉" in reason for reason in contextual.hard_blocks)
    assert contextual.action.startswith("기다림")
    assert contextual.confidence != "높음"
    assert contextual.risk_score == len(contextual.risks) + 2 * len(contextual.hard_blocks)


def test_no_observed_resistance_does_not_invent_a_target() -> None:
    frame = make_frame()
    # Each high is the closing price, so the latest close is also the all-time
    # high in this strictly increasing fixture. There is no overhead price.
    close = np.linspace(50, 100, len(frame))
    frame["close"] = frame["high"] = close
    frame["open"] = close - 0.1
    frame["low"] = close - 0.5
    reading = analyze_ichimoku(frame)
    assert reading.first_target_price is None
    assert any("목표 후보" in reason for reason in reading.hard_blocks)
    assert reading.action.startswith("기다림")

