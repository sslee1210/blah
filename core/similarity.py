from __future__ import annotations

"""Research-only nearest historical pattern outcomes without look-ahead."""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .ichimoku import MIN_BARS, calculate_ichimoku


FEATURES = (
    "rsi14",
    "close_sma5_pct",
    "close_sma20_pct",
    "close_sma60_pct",
    "sma5_sma20_pct",
    "sma20_sma60_pct",
    "return5_pct",
    "return20_pct",
    "price_kijun_pct",
    "tenkan_kijun_pct",
    "price_cloud_top_pct",
    "cloud_width_pct",
    "volume_ratio",
    "candle_range_pct",
    "atr14_pct",
    "adx14",
    "di_spread14",
)

MIN_RELIABLE_SAMPLE = 25
MAX_NEIGHBORS = 30
OUTCOME_HORIZON = 10
REQUIRED_ICHIMOKU_STATES = (
    "price_position",
    "future_cloud",
    "tk_structure",
    "kijun_slope",
    "chikou_state",
)


@dataclass(frozen=True)
class SimilarityResult:
    sample_count: int
    up_count: int
    up_rate: float | None
    median_return_pct: float | None
    recent_sample_count: int
    recent_up_count: int
    recent_up_rate: float | None
    nearest_distance: float | None
    status: str
    match_level: str = "확인 불가"
    candidate_count: int = 0
    average_up_return_pct: float | None = None
    average_down_return_pct: float | None = None
    invalidation_count: int = 0
    invalidation_rate: float | None = None
    expected_return_pct: float | None = None
    downside_p25_pct: float | None = None
    payoff_ratio: float | None = None


def feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = calculate_ichimoku(frame)
    close = result["close"]
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    rsi = rsi.mask((gain == 0) & (loss > 0), 0.0)
    rsi = rsi.mask((gain == 0) & (loss == 0), 50.0)
    sma5 = close.rolling(5).mean()
    sma20 = close.rolling(20).mean()
    sma60 = close.rolling(60).mean()
    output = pd.DataFrame(index=result.index)
    output["rsi14"] = rsi
    output["close_sma5_pct"] = _pct(close, sma5)
    output["close_sma20_pct"] = _pct(close, sma20)
    output["close_sma60_pct"] = _pct(close, sma60)
    output["sma5_sma20_pct"] = _pct(sma5, sma20)
    output["sma20_sma60_pct"] = _pct(sma20, sma60)
    output["return5_pct"] = close.pct_change(5) * 100.0
    output["return20_pct"] = close.pct_change(20) * 100.0
    output["price_kijun_pct"] = _pct(close, result["kijun"])
    output["tenkan_kijun_pct"] = _pct(result["tenkan"], result["kijun"])
    output["price_cloud_top_pct"] = _pct(close, result["cloud_top"])
    output["cloud_width_pct"] = (
        (result["cloud_top"] - result["cloud_bottom"]) / close.replace(0, np.nan) * 100.0
    )
    output["volume_ratio"] = result["volume_ratio"]
    output["candle_range_pct"] = (
        (result["high"] - result["low"]) / close.replace(0, np.nan) * 100.0
    )
    output["atr14_pct"] = result["atr14"] / close.replace(0, np.nan) * 100.0
    output["adx14"] = result["adx14"]
    output["di_spread14"] = result["plus_di14"] - result["minus_di14"]
    output["price_position"] = np.where(
        close > result["cloud_top"],
        "above",
        np.where(close < result["cloud_bottom"], "below", "inside"),
    )
    output["future_cloud"] = np.select(
        [result["span_a_raw"] > result["span_b_raw"], result["span_a_raw"] < result["span_b_raw"]],
        ["bullish", "bearish"],
        default="neutral",
    )
    output["tk_structure"] = np.where(
        result["tenkan"] > result["kijun"],
        "tenkan_above",
        np.where(result["tenkan"] < result["kijun"], "tenkan_below", "equal"),
    )
    kijun_change = (result["kijun"] / result["kijun"].shift(5) - 1.0) * 100.0
    output["kijun_slope"] = np.where(
        kijun_change > 0.15, "rising", np.where(kijun_change < -0.15, "falling", "flat")
    )
    past_high = result["high"].shift(26)
    past_close = result["close"].shift(26)
    past_cloud_top = result["cloud_top"].shift(26)
    past_cloud_bottom = result["cloud_bottom"].shift(26)
    chikou_strong = (close > past_high) & (past_cloud_top.isna() | (close > past_cloud_top))
    chikou_partial = close > past_close
    chikou_weak = (close < past_close) & (
        past_cloud_bottom.isna() | (close < past_cloud_bottom)
    )
    output["chikou_state"] = np.select(
        [chikou_strong, chikou_partial, chikou_weak],
        ["strong", "partial", "weak"],
        default="mixed",
    )
    # Historical matches must have enough data for the same Ichimoku reading
    # used today. Before the 52+26 cloud is ready, pandas' row-wise max/min can
    # otherwise make a half-cloud look complete and admit false matches.
    output.loc[output.index[: MIN_BARS - 1], list(FEATURES)] = np.nan

    support_columns = ["tenkan", "kijun", "cloud_top", "cloud_bottom"]
    supports = result[support_columns].where(result[support_columns].lt(close, axis=0))
    nearest_support = supports.max(axis=1).fillna(close * 0.94)
    output["research_invalidation_price"] = nearest_support * 0.99
    future_lows = pd.concat(
        [result["low"].shift(-offset) for offset in range(1, OUTCOME_HORIZON + 1)], axis=1
    )
    output["future_low_10"] = future_lows.min(axis=1, skipna=False)
    output["invalidation_hit"] = (
        output["future_low_10"] <= output["research_invalidation_price"]
    ).astype("boolean").mask(output["future_low_10"].isna())
    output["entry"] = result["open"].shift(-1)
    output["exit"] = result["close"].shift(-OUTCOME_HORIZON)
    output["forward_return_pct"] = (output["exit"] / output["entry"] - 1.0) * 100.0
    return output.replace([np.inf, -np.inf], np.nan)


def summarize_similarity(frame: pd.DataFrame, *, neighbors: int = MAX_NEIGHBORS) -> SimilarityResult:
    features = feature_frame(frame)
    current = features.iloc[-1]
    history = features.iloc[:-OUTCOME_HORIZON].dropna(subset=[*FEATURES, "forward_return_pct"])
    if any(not _finite(current.get(feature)) for feature in FEATURES) or len(history) < 25:
        return SimilarityResult(0, 0, None, None, 0, 0, None, None, "표본 부족")

    same_context, match_level = _context_candidates(history, current)
    candidate_count = len(same_context)

    if same_context.empty:
        return SimilarityResult(
            0,
            0,
            None,
            None,
            0,
            0,
            None,
            None,
            "일목 핵심 구조가 모두 같은 과거 표본 없음",
            match_level=match_level,
            candidate_count=0,
        )

    matrix = same_context.loc[:, FEATURES].astype(float)
    current_vector = current.loc[list(FEATURES)].astype(float)
    median = matrix.median(axis=0)
    mad = (matrix - median).abs().median(axis=0) * 1.4826
    std = matrix.std(axis=0, ddof=0)
    scale = mad.where(mad > 1e-8, std).where(lambda values: values > 1e-8, 1.0)
    differences = ((matrix - current_vector) / scale).abs()
    match_scores = pd.DataFrame(
        {
            "largest_indicator_difference": differences.max(axis=1),
            "average_indicator_difference": differences.mean(axis=1),
        },
        index=matrix.index,
    )
    count = min(max(10, neighbors), len(match_scores))
    ordered_indices = match_scores.sort_values(
        ["largest_indicator_difference", "average_indicator_difference"]
    ).index
    nearest_indices = _purged_neighbor_indices(
        ordered_indices,
        all_index=features.index,
        limit=count,
        embargo=OUTCOME_HORIZON,
    )
    selected = same_context.loc[nearest_indices].copy()
    selected["distance"] = match_scores.loc[nearest_indices, "largest_indicator_difference"]

    returns = selected["forward_return_pct"].astype(float)
    up = returns > 0
    down = ~up
    sample_count = int(len(returns))
    up_count = int(up.sum())
    invalidation_count = int(selected["invalidation_hit"].astype(bool).sum())
    recent_count = max(1, math.ceil(sample_count * 0.25))
    recent = selected.sort_index().tail(recent_count)["forward_return_pct"].astype(float)
    recent_up = int((recent > 0).sum())
    rate = up_count / sample_count * 100.0 if sample_count else None
    recent_rate = recent_up / len(recent) * 100.0 if len(recent) else None
    if sample_count < 15:
        status = "표본 적음"
    elif sample_count < MIN_RELIABLE_SAMPLE:
        status = "표본 주의"
    elif rate is not None and rate >= 65 and recent_rate is not None and recent_rate >= 50:
        status = "과거 표본은 양호"
    elif rate is not None and rate < 45:
        status = "과거 표본은 불리"
    else:
        status = "뚜렷한 우위 없음"
    return SimilarityResult(
        sample_count=sample_count,
        up_count=up_count,
        up_rate=rate,
        median_return_pct=float(returns.median()) if sample_count else None,
        recent_sample_count=int(len(recent)),
        recent_up_count=recent_up,
        recent_up_rate=recent_rate,
        nearest_distance=float(selected["distance"].min()),
        status=status,
        match_level=match_level,
        candidate_count=candidate_count,
        average_up_return_pct=float(returns[up].mean()) if up.any() else None,
        average_down_return_pct=float(returns[down].mean()) if down.any() else None,
        invalidation_count=invalidation_count,
        invalidation_rate=invalidation_count / sample_count * 100.0 if sample_count else None,
        expected_return_pct=float(returns.mean()) if sample_count else None,
        downside_p25_pct=float(returns.quantile(0.25)) if sample_count else None,
        payoff_ratio=(
            float(returns[up].mean() / abs(returns[down].mean()))
            if up.any() and down.any() and float(returns[down].mean()) != 0
            else None
        ),
    )


def _context_candidates(
    history: pd.DataFrame, current: pd.Series
) -> tuple[pd.DataFrame, str]:
    mask = pd.Series(True, index=history.index)
    for column in REQUIRED_ICHIMOKU_STATES:
        mask &= history[column] == current[column]
    return history[mask], "일목 핵심 구조 모두 일치"


def _pct(left: pd.Series, right: pd.Series) -> pd.Series:
    return (left / right.replace(0, np.nan) - 1.0) * 100.0


def _purged_neighbor_indices(
    ordered_indices: pd.Index,
    *,
    all_index: pd.Index,
    limit: int,
    embargo: int,
) -> pd.Index:
    """Keep nearest matches whose forward outcome windows do not overlap."""

    positions = {value: position for position, value in enumerate(all_index)}
    selected: list[object] = []
    selected_positions: list[int] = []
    for value in ordered_indices:
        position = positions.get(value)
        if position is None:
            continue
        if any(abs(position - previous) <= embargo for previous in selected_positions):
            continue
        selected.append(value)
        selected_positions.append(position)
        if len(selected) >= limit:
            break
    return pd.Index(selected)


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
