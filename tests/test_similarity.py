from __future__ import annotations

import numpy as np
import pandas as pd

from core.similarity import (
    FEATURES,
    _context_candidates,
    _purged_neighbor_indices,
    feature_frame,
    summarize_similarity,
)


def make_random_frame(rows: int = 620) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    index = pd.bdate_range("2021-01-04", periods=rows)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.011, rows)))
    frame = pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, rows)),
            "high": close * (1 + rng.uniform(0.002, 0.02, rows)),
            "low": close * (1 - rng.uniform(0.002, 0.02, rows)),
            "close": close,
            "volume": rng.integers(500_000, 4_000_000, rows),
        },
        index=index,
    )
    # Generated opens must also be within the candle's high/low range.
    frame["high"] = frame[["high", "open", "close"]].max(axis=1)
    frame["low"] = frame[["low", "open", "close"]].min(axis=1)
    frame["trade_value"] = frame["close"] * frame["volume"]
    return frame


def test_all_similarity_features_are_calculated() -> None:
    features = feature_frame(make_random_frame())
    current = features.iloc[-1]
    assert all(pd.notna(current[name]) for name in FEATURES)
    assert all(
        pd.notna(current[name])
        for name in ("price_position", "future_cloud", "tk_structure", "kijun_slope", "chikou_state")
    )
    assert pd.notna(current["atr14_pct"])
    assert pd.notna(current["adx14"])


def test_forward_label_is_next_open_to_tenth_close() -> None:
    frame = make_random_frame()
    features = feature_frame(frame)
    index = 200
    expected = (frame["close"].iloc[index + 10] / frame["open"].iloc[index + 1] - 1) * 100
    assert features["forward_return_pct"].iloc[index] == expected
    assert pd.notna(features["research_invalidation_price"].iloc[index])
    assert isinstance(bool(features["invalidation_hit"].iloc[index]), bool)


def test_similarity_returns_nearest_research_sample_without_claiming_guarantee() -> None:
    result = summarize_similarity(make_random_frame())
    assert 10 <= result.sample_count <= min(30, result.candidate_count)
    assert result.up_rate is not None
    assert 0 <= result.up_rate <= 100
    assert result.candidate_count >= result.sample_count
    assert result.average_up_return_pct is None or result.average_up_return_pct > 0
    assert result.average_down_return_pct is None or result.average_down_return_pct <= 0
    assert result.invalidation_rate is not None
    assert 0 <= result.invalidation_rate <= 100
    assert result.expected_return_pct is not None
    assert result.downside_p25_pct is not None
    assert "보장" not in result.status


def test_context_selection_requires_every_ichimoku_state_to_match() -> None:
    history = pd.DataFrame(
        {
            "price_position": ["above"] * 40,
            "future_cloud": ["bullish"] * 40,
            "tk_structure": ["tenkan_above"] * 40,
            "kijun_slope": ["rising"] * 20 + ["flat"] * 20,
            "chikou_state": ["strong"] * 20 + ["partial"] * 20,
        }
    )
    current = pd.Series(
        {
            "price_position": "above",
            "future_cloud": "bullish",
            "tk_structure": "tenkan_above",
            "kijun_slope": "rising",
            "chikou_state": "strong",
        }
    )
    selected, level = _context_candidates(history, current)
    assert level == "일목 핵심 구조 모두 일치"
    assert len(selected) == 20


def test_neighbor_selection_embargoes_overlapping_forward_windows() -> None:
    index = pd.bdate_range("2026-01-01", periods=60)
    ordered = pd.Index([index[20], index[21], index[25], index[31], index[45]])
    selected = _purged_neighbor_indices(ordered, all_index=index, limit=3, embargo=10)
    positions = [index.get_loc(value) for value in selected]
    assert positions == [20, 31, 45]


def test_partial_cloud_warmup_cannot_be_used_as_a_historical_match() -> None:
    from core.ichimoku import MIN_BARS

    features = feature_frame(make_random_frame())
    eligible = features.dropna(subset=[*FEATURES, "forward_return_pct"])
    assert eligible.index[0] == features.index[MIN_BARS - 1]


def test_unfinished_future_outcomes_remain_unknown() -> None:
    features = feature_frame(make_random_frame())
    assert features["future_low_10"].iloc[-10:].isna().all()
    assert features["invalidation_hit"].iloc[-10:].isna().all()
    assert features["forward_return_pct"].iloc[-10:].isna().all()
    assert pd.notna(features["invalidation_hit"].iloc[-11])


def test_features_and_context_use_only_information_available_at_that_time() -> None:
    from core.similarity import REQUIRED_ICHIMOKU_STATES

    frame = make_random_frame()
    full = feature_frame(frame)
    truncated = feature_frame(frame.iloc[:300])
    columns = [*FEATURES, *REQUIRED_ICHIMOKU_STATES, "research_invalidation_price"]
    pd.testing.assert_series_equal(full.iloc[299][columns], truncated.iloc[-1][columns])
