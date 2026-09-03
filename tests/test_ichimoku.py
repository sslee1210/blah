from __future__ import annotations

import numpy as np
import pandas as pd

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


def test_weekly_resampling_preserves_ohlcv_meaning() -> None:
    weekly = resample_weekly(make_frame())
    assert len(weekly) >= 100
    assert (weekly["high"] >= weekly[["open", "close"]].max(axis=1)).all()
    assert (weekly["low"] <= weekly[["open", "close"]].min(axis=1)).all()
    assert (weekly["volume"] > 0).all()


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
