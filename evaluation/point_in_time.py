from __future__ import annotations

"""Strict point-in-time replay and evaluation for the existing analyzers.

The production analyzer remains the source of truth.  Every decision is made
from a prefix ending at ``as_of``.  The untouched full frame is used only by
``_forward_outcomes`` after the decision has been recorded.
"""

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, time, timezone
import json
import math
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.ichimoku import (
    MIN_BARS,
    IchimokuReading,
    analyze_ichimoku,
    apply_context,
    prepare_ohlcv,
    resample_weekly,
)
from core.domestic_kiwoom_rest import completed_domestic_daily_bars
from core.kiwoom_rest import StockInfo, completed_daily_bars
from core.market_intelligence import MarketIntelligence, _combine_environment_score
from core.reporting import AnalyzedStock
from core.similarity import FEATURES, SimilarityResult, feature_frame, summarize_similarity
from data_pipeline.point_in_time import price_views
from domestic_stock_analyzer import (
    MIN_AVG_TRADE_VALUE as KR_MIN_AVG_TRADE_VALUE,
    MIN_AVG_VOLUME as KR_MIN_AVG_VOLUME,
    _apply_domestic_context,
    _liquidity as _domestic_liquidity,
)
from us_ichimoku_analyzer import (
    MIN_AVG_DOLLAR_VOLUME as US_MIN_AVG_DOLLAR_VOLUME,
    MIN_AVG_VOLUME as US_MIN_AVG_VOLUME,
    _liquidity as _us_liquidity,
)


HORIZONS = (1, 5, 10, 20)
RANKING_HORIZONS = (5, 10, 20)
GRADES = ("A+", "A", "B", "C", "D")
ACTIONS = ("관심", "기다림", "피하기")
ABLATION_FAMILIES = (
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
)


@dataclass(frozen=True)
class BacktestConfig:
    market: str
    baseline_mode: str = "TECHNICAL_BASELINE"
    dataset_point_in_time_verified: bool = False
    price_policy_verified: bool = False
    delisted_securities_included: bool = False
    exchange_calendar_verified: bool = False
    horizons: tuple[int, ...] = HORIZONS
    decision_step: int = 1
    min_history: int = MIN_BARS
    min_cross_section: int = 20
    large_loss_threshold_pct: float = -10.0
    purge_sessions: int = 20
    min_split_dates: int = 60
    include_similarity_features: bool = True

    def __post_init__(self) -> None:
        market = self.market.upper()
        if market not in {"US", "KR"}:
            raise ValueError("market은 US 또는 KR이어야 합니다.")
        if self.decision_step < 1:
            raise ValueError("decision_step은 1 이상이어야 합니다.")
        if self.min_history < MIN_BARS:
            raise ValueError(f"min_history는 {MIN_BARS} 이상이어야 합니다.")
        if not self.horizons or any(item < 1 for item in self.horizons):
            raise ValueError("horizons에는 1 이상의 값이 필요합니다.")
        if self.baseline_mode not in {"TECHNICAL_BASELINE", "FULL_CONTEXT_BASELINE"}:
            raise ValueError("baseline_mode는 TECHNICAL_BASELINE 또는 FULL_CONTEXT_BASELINE이어야 합니다.")


@dataclass
class EvaluationBundle:
    signals: pd.DataFrame = field(default_factory=pd.DataFrame)
    outcomes: pd.DataFrame = field(default_factory=pd.DataFrame)
    baseline: pd.DataFrame = field(default_factory=pd.DataFrame)
    grade_results: pd.DataFrame = field(default_factory=pd.DataFrame)
    action_results: pd.DataFrame = field(default_factory=pd.DataFrame)
    action_ordering: pd.DataFrame = field(default_factory=pd.DataFrame)
    rank_ic: pd.DataFrame = field(default_factory=pd.DataFrame)
    ranking_groups: pd.DataFrame = field(default_factory=pd.DataFrame)
    walk_forward: pd.DataFrame = field(default_factory=pd.DataFrame)
    ablation: pd.DataFrame = field(default_factory=pd.DataFrame)
    signal_correlations: pd.DataFrame = field(default_factory=pd.DataFrame)
    similarity_validation: pd.DataFrame = field(default_factory=pd.DataFrame)
    metadata: dict[str, object] = field(default_factory=dict)


def canonical_action(value: str) -> str:
    for action in ACTIONS:
        if str(value).startswith(action):
            return action
    return "기타"


def evaluate_point_in_time(
    stocks: Mapping[str, tuple[StockInfo, pd.DataFrame]],
    *,
    config: BacktestConfig,
    market_proxies: Mapping[str, pd.DataFrame] | None = None,
    intelligence_snapshots: Mapping[tuple[str, str], MarketIntelligence] | None = None,
    universe_snapshots: Mapping[str, set[str]] | None = None,
) -> EvaluationBundle:
    """Replay production decisions and calculate future outcomes.

    ``stocks`` must contain only the universe that was eligible on each replay
    date.  A static, present-day universe is accepted mechanically but marked
    as survivorship-biased in metadata; callers must not label that result a
    trusted baseline.

    Intelligence snapshots are keyed by ``(YYYY-MM-DD, symbol)``.  A market
    snapshot may use ``*`` as the symbol.  No live news request is made here.
    """

    errors: list[dict[str, str]] = []
    proxies: dict[str, pd.DataFrame] = {}
    for key, value in (market_proxies or {}).items():
        try:
            proxies[key.upper()] = _validated_frame(price_views(value)[0], config.market)
        except Exception as exc:
            # A short pilot dataset can legitimately contain fewer than the
            # indicator warm-up bars for its market proxy.  Treat that proxy as
            # unavailable and let the trust/data-requirement gates report the
            # run as not evaluable instead of aborting the entire evaluation.
            errors.append({"symbol": f"proxy:{key}", "error": str(exc)})
    intelligence = intelligence_snapshots or {}
    universe = {
        str(day): {symbol.upper() for symbol in symbols}
        for day, symbols in (universe_snapshots or {}).items()
    }
    signal_rows: list[dict[str, object]] = []
    outcome_rows: list[dict[str, object]] = []
    proxy_cache: dict[pd.Timestamp, tuple[str, bool]] = {}

    for key, (stock, raw_frame) in stocks.items():
        try:
            signal_raw, outcome_raw, _ = price_views(raw_frame)
            frame = _validated_frame(signal_raw, config.market)
            outcome_frame = _validated_frame(outcome_raw, config.market)
            if not frame.index.equals(outcome_frame.index):
                raise ValueError("신호 가격과 성과 가격의 거래일 인덱스가 다릅니다.")
        except Exception as exc:
            errors.append({"symbol": stock.symbol or key, "error": str(exc)})
            continue
        last_decision_position = len(frame) - max(config.horizons) - 1
        if last_decision_position < config.min_history - 1:
            errors.append(
                {
                    "symbol": stock.symbol or key,
                    "error": "지표 준비 구간과 최대 Forward 구간을 함께 확보하지 못했습니다.",
                }
            )
            continue

        positions = range(
            config.min_history - 1,
            last_decision_position + 1,
            config.decision_step,
        )
        for position in positions:
            history = frame.iloc[: position + 1].copy()
            as_of = pd.Timestamp(history.index[-1]).normalize()
            if universe and stock.symbol.upper() not in universe.get(
                as_of.date().isoformat(), set()
            ):
                continue
            try:
                raw_daily = analyze_ichimoku(history, timeframe="일봉")
                weekly = _weekly_reading(history)
                if as_of not in proxy_cache:
                    proxy_cache[as_of] = _market_context_as_of(
                        proxies,
                        as_of=as_of,
                        market=config.market,
                    )
                market_label, market_weak = proxy_cache[as_of]
                contextual = _apply_market_context(
                    raw_daily,
                    weekly=weekly,
                    market=config.market,
                    market_label=market_label,
                    market_weak=market_weak,
                )
                liquid, liquidity_reason = _liquidity(contextual, config.market)
                if not liquid:
                    contextual = _apply_liquidity_block(
                        contextual, liquidity_reason, config.market
                    )
                similarity = summarize_similarity(history)
                snapshot = _intelligence_for(intelligence, as_of, stock.symbol)
                analyzed = AnalyzedStock(
                    stock=stock,
                    daily=contextual,
                    similarity=similarity,
                    liquid=liquid,
                    liquidity_reason=liquidity_reason,
                    intelligence=snapshot,
                )
                features = _current_similarity_features(history, config)
                signal = _signal_row(
                    analyzed,
                    raw_daily=raw_daily,
                    weekly=weekly,
                    as_of=as_of,
                    market=config.market,
                    market_label=market_label,
                    market_weak=market_weak,
                    features=features,
                )
                signal_rows.append(signal)
                outcome_rows.extend(
                    _forward_outcomes(
                        signal,
                        frame=outcome_frame,
                        signal_frame=frame,
                        position=position,
                        horizons=config.horizons,
                        invalidation_price=raw_daily.invalidation_price,
                        large_loss_threshold_pct=config.large_loss_threshold_pct,
                    )
                )
            except Exception as exc:
                errors.append(
                    {
                        "symbol": stock.symbol or key,
                        "as_of": as_of.date().isoformat(),
                        "error": str(exc),
                    }
                )

    signals = pd.DataFrame(signal_rows)
    outcomes = pd.DataFrame(outcome_rows)
    metadata = _metadata(
        stocks=stocks,
        proxies=proxies,
        config=config,
        signals=signals,
        outcomes=outcomes,
        intelligence=intelligence,
        universe=universe,
        errors=errors,
    )
    if signals.empty or outcomes.empty:
        if signals.empty:
            signals = pd.DataFrame(columns=_signal_columns())
        if outcomes.empty:
            outcomes = pd.DataFrame(
                columns=[
                    *_signal_columns(),
                    "horizon",
                    "entry_price",
                    "exit_price",
                    "forward_return_pct",
                    "mae_pct",
                    "mfe_pct",
                    "max_drawdown_pct",
                    "invalidation_hit",
                    "large_loss",
                    "split",
                ]
            )
        empty = _empty_result_tables(config)
        return EvaluationBundle(
            signals=signals,
            outcomes=outcomes,
            baseline=empty["baseline"],
            grade_results=empty["grade_results"],
            action_results=empty["action_results"],
            action_ordering=empty["action_ordering"],
            rank_ic=empty["rank_ic"],
            ranking_groups=empty["ranking_groups"],
            walk_forward=empty["walk_forward"],
            ablation=empty["ablation"],
            signal_correlations=empty["signal_correlations"],
            similarity_validation=empty["similarity_validation"],
            metadata=metadata,
        )

    signals, outcomes, split_meta = apply_walk_forward_splits(
        signals,
        outcomes,
        purge_sessions=config.purge_sessions,
        min_split_dates=config.min_split_dates,
    )
    metadata["walk_forward"] = split_meta
    _finalize_trust(metadata, split_meta)
    baseline = aggregate_performance(outcomes, [])
    grade_results = aggregate_performance(outcomes, ["grade"])
    action_results = aggregate_performance(outcomes, ["action"])
    action_ordering = action_ordering_test(action_results)
    rank_ic, ranking_groups = ranking_performance(
        outcomes,
        min_cross_section=config.min_cross_section,
    )
    walk_forward = aggregate_performance(
        outcomes.loc[outcomes["split"].isin(("train", "validation", "test"))],
        ["split", "action"],
    )
    ablation = ablation_performance(outcomes)
    correlations = signal_correlation_table(signals)
    similarity = similarity_performance(outcomes)
    return EvaluationBundle(
        signals=signals,
        outcomes=outcomes,
        baseline=baseline,
        grade_results=grade_results,
        action_results=action_results,
        action_ordering=action_ordering,
        rank_ic=rank_ic,
        ranking_groups=ranking_groups,
        walk_forward=walk_forward,
        ablation=ablation,
        signal_correlations=correlations,
        similarity_validation=similarity,
        metadata=metadata,
    )


def _validated_frame(frame: pd.DataFrame, market: str) -> pd.DataFrame:
    # prepare_ohlcv also sorts and rejects duplicate/corrupt price rows.  Keep
    # the returned full frame separate from every decision prefix.
    completed = (
        completed_daily_bars(frame)
        if market.upper() == "US"
        else completed_domestic_daily_bars(frame)
    )
    return prepare_ohlcv(completed)


def _weekly_reading(history: pd.DataFrame) -> IchimokuReading | None:
    try:
        weekly = resample_weekly(history)
        if len(weekly) < MIN_BARS:
            return None
        return analyze_ichimoku(weekly, timeframe="주봉")
    except ValueError:
        return None


def _market_context_as_of(
    proxies: Mapping[str, pd.DataFrame],
    *,
    as_of: pd.Timestamp,
    market: str,
) -> tuple[str, bool]:
    required = ("SPY", "QQQ") if market.upper() == "US" else ("KOSPI", "KOSDAQ")
    readings: list[IchimokuReading] = []
    for name in required:
        frame = proxies.get(name)
        if frame is None:
            continue
        prefix = frame.loc[pd.DatetimeIndex(frame.index).normalize() <= as_of]
        if len(prefix) < MIN_BARS:
            continue
        readings.append(analyze_ichimoku(prefix, timeframe=f"{name} 일봉"))
    prefix = "SPY·QQQ" if market.upper() == "US" else "KOSPI·KOSDAQ"
    if len(readings) < 2:
        return f"{prefix} 일부 확인 불가", False
    strong = sum(
        item.price_position == "구름 위" and item.grade in {"A+", "A"} for item in readings
    )
    weak = sum(item.price_position == "구름 아래" or item.grade == "D" for item in readings)
    if strong == 2:
        return f"{prefix} 모두 상승 방향", False
    if weak == 2:
        return f"{prefix} 모두 약세", True
    return f"{prefix} 방향이 엇갈림", False


def _apply_market_context(
    reading: IchimokuReading,
    *,
    weekly: IchimokuReading | None,
    market: str,
    market_label: str,
    market_weak: bool,
) -> IchimokuReading:
    if market.upper() == "US":
        return apply_context(
            reading,
            weekly=weekly,
            market_label=market_label,
            market_is_weak=market_weak,
        )
    return _apply_domestic_context(
        reading,
        weekly=weekly,
        market_label=market_label,
        market_is_weak=market_weak,
    )


def _liquidity(reading: IchimokuReading, market: str) -> tuple[bool, str]:
    if market.upper() == "US":
        return _us_liquidity(reading)
    return _domestic_liquidity(reading)


def _apply_liquidity_block(
    reading: IchimokuReading, reason: str, market: str
) -> IchimokuReading:
    blocks = tuple(dict.fromkeys((*reading.hard_blocks, reason)))
    risks = tuple(dict.fromkeys((*reading.risks, reason)))
    if market.upper() == "US":
        confidence = "낮음" if reading.confidence == "낮음" else "보통"
    else:
        confidence = "보통" if reading.confidence == "높음" else reading.confidence
    return replace(
        reading,
        hard_blocks=blocks,
        risks=risks,
        risk_score=len(risks) + len(blocks) * 2,
        confidence=confidence,
        action="피하기 - 거래량·거래대금이 부족해 매매 후보에서 제외",
    )


def _intelligence_for(
    snapshots: Mapping[tuple[str, str], MarketIntelligence],
    as_of: pd.Timestamp,
    symbol: str,
) -> MarketIntelligence | None:
    day = as_of.date().isoformat()
    value = snapshots.get((day, symbol.upper())) or snapshots.get((day, "*"))
    if value is None or not value.generated_at:
        return None
    try:
        generated = pd.Timestamp(value.generated_at)
        if generated.tzinfo is None:
            return None
        market = value.market.upper()
        zone = ZoneInfo("America/New_York" if market == "US" else "Asia/Seoul")
        close_time = time(16, 0) if market == "US" else time(15, 30)
        cutoff = pd.Timestamp(datetime.combine(as_of.date(), close_time, tzinfo=zone))
        if generated.tz_convert(zone) > cutoff:
            return None
    except (TypeError, ValueError, KeyError):
        return None
    return value


def _current_similarity_features(
    history: pd.DataFrame, config: BacktestConfig
) -> dict[str, float | None]:
    if not config.include_similarity_features:
        return {}
    current = feature_frame(history).iloc[-1]
    return {
        f"similarity_feature_{name}": (
            float(current[name]) if pd.notna(current.get(name)) else None
        )
        for name in FEATURES
    }


def _signal_row(
    analyzed: AnalyzedStock,
    *,
    raw_daily: IchimokuReading,
    weekly: IchimokuReading | None,
    as_of: pd.Timestamp,
    market: str,
    market_label: str,
    market_weak: bool,
    features: Mapping[str, float | None],
) -> dict[str, object]:
    daily = analyzed.daily
    similarity = analyzed.similarity
    intelligence = analyzed.intelligence
    row: dict[str, object] = {
        "market": market.upper(),
        "as_of": as_of.date().isoformat(),
        "symbol": analyzed.stock.symbol,
        "exchange": analyzed.stock.exchange,
        "name": analyzed.stock.display_name,
        "grade": daily.grade,
        "action": canonical_action(analyzed.final_action),
        "action_text": analyzed.final_action,
        "technical_score": analyzed.technical_score,
        "context_adjusted_score": analyzed.context_adjusted_score,
        "confidence": daily.confidence,
        "bullish_score": raw_daily.bullish_score,
        "bearish_score": raw_daily.bearish_score,
        "risk_score": daily.risk_score,
        "hard_block_count": len(daily.hard_blocks),
        "liquid": analyzed.liquid,
        "liquidity_reason": analyzed.liquidity_reason,
        "market_context": market_label,
        "market_weak": market_weak,
        "price_above_cloud": raw_daily.price_position == "구름 위",
        "tenkan_above_kijun": raw_daily.tk_state in {"전환선 우위", "최근 상향 교차"},
        "kijun_nonfalling": raw_daily.kijun_slope in {"상승", "수평"},
        "chikou_strong": raw_daily.chikou_state == "강세 확인",
        "future_cloud_bullish": raw_daily.future_cloud == "양의 구름",
        "adx_di_bullish": raw_daily.adx14 >= 25.0 and raw_daily.plus_di14 > raw_daily.minus_di14,
        "volume_confirmed": raw_daily.volume_ratio is not None and raw_daily.volume_ratio >= 1.0,
        "weekly_bullish": weekly is not None
        and weekly.grade in {"A+", "A"}
        and weekly.price_position == "구름 위",
        "market_bullish": "모두 상승" in market_label,
        "atr_extension_block": raw_daily.candle_range_atr > 2.5
        or (raw_daily.atr14 > 0 and abs(raw_daily.close - raw_daily.kijun) / raw_daily.atr14 > 3.0),
        "similar_sample_count": similarity.sample_count,
        "similar_up_rate": similarity.up_rate,
        "similar_nearest_distance": similarity.nearest_distance,
        "similar_status": similarity.status,
        "similar_match_level": similarity.match_level,
        "environment_score": intelligence.score if intelligence else None,
        "market_score": intelligence.market_score if intelligence else None,
        "news_score": intelligence.news_score if intelligence else None,
        "event_risk": intelligence.event_risk if intelligence else None,
        "intelligence_generated_at": intelligence.generated_at if intelligence else "",
    }
    row.update(features)
    row.update(
        _ablation_projection(
            analyzed,
            raw_daily=raw_daily,
            weekly=weekly,
            market=market,
            market_label=market_label,
            market_weak=market_weak,
        )
    )
    return row


def _ablation_projection(
    analyzed: AnalyzedStock,
    *,
    raw_daily: IchimokuReading,
    weekly: IchimokuReading | None,
    market: str,
    market_label: str,
    market_weak: bool,
) -> dict[str, object]:
    """Record evaluation-only leave-one-family-out projections.

    The live decision is never mutated.  Where a family has no operational
    effect (60-minute bars and similarity), the projection is exactly the
    baseline.  Entangled families that cannot be disabled without rewriting
    the production model are explicitly marked non-identifiable instead of
    inventing a counterfactual score.
    """

    result: dict[str, object] = {}
    base_action = canonical_action(analyzed.final_action)
    base_score = analyzed.context_adjusted_score
    for family in ABLATION_FAMILIES:
        slug = _family_slug(family)
        result[f"ablation_{slug}_action"] = base_action
        result[f"ablation_{slug}_score"] = base_score
        result[f"ablation_{slug}_status"] = "projected"

    for family in ("60분봉", "과거 유사패턴"):
        slug = _family_slug(family)
        result[f"ablation_{slug}_status"] = "no_operational_effect"

    for family, disabled in (
        ("ADX / DI", "adx_di"),
        ("ATR", "atr"),
        ("거래량", "volume"),
    ):
        ablated_raw = _ablate_raw_reading(raw_daily, disabled)
        ablated_context = _apply_market_context(
            ablated_raw,
            weekly=weekly,
            market=market,
            market_label=market_label,
            market_weak=market_weak,
        )
        variant = _analyzed_with_liquidity(
            analyzed,
            ablated_context,
            market=market,
            ignore_trade_value=False,
            ignore_volume=disabled == "volume",
        )
        slug = _family_slug(family)
        result[f"ablation_{slug}_action"] = canonical_action(variant.final_action)
        result[f"ablation_{slug}_score"] = variant.context_adjusted_score
        result[f"ablation_{slug}_status"] = "evaluation_only_rule_neutralization"

    # Weekly and market-direction gates can be neutralized exactly without
    # touching the production functions: feed them a passing context while
    # keeping every other input fixed.
    passing_weekly = replace(raw_daily, grade="A+", price_position="구름 위")
    no_weekly_daily = _apply_market_context(
        raw_daily,
        weekly=passing_weekly,
        market=market,
        market_label=market_label,
        market_weak=market_weak,
    )
    no_weekly = _analyzed_with_liquidity(
        analyzed,
        no_weekly_daily,
        market=market,
        ignore_trade_value=False,
        ignore_volume=False,
    )
    slug = _family_slug("주봉 확인")
    result[f"ablation_{slug}_action"] = canonical_action(no_weekly.final_action)
    result[f"ablation_{slug}_score"] = no_weekly.context_adjusted_score
    result[f"ablation_{slug}_status"] = "exact_context_neutralization"

    passing_label = (
        "SPY·QQQ 모두 상승 방향"
        if market.upper() == "US"
        else "KOSPI·KOSDAQ 모두 상승 방향"
    )
    no_market_daily = _apply_market_context(
        raw_daily,
        weekly=weekly,
        market=market,
        market_label=passing_label,
        market_weak=False,
    )
    no_market = _analyzed_with_liquidity(
        analyzed,
        no_market_daily,
        market=market,
        ignore_trade_value=False,
        ignore_volume=False,
    )
    slug = _family_slug("시장 방향")
    result[f"ablation_{slug}_action"] = canonical_action(no_market.final_action)
    result[f"ablation_{slug}_score"] = no_market.context_adjusted_score
    result[f"ablation_{slug}_status"] = "exact_context_neutralization"

    # Trade value is only an independent liquidity gate.  Ignore that gate
    # while retaining the volume requirement and every technical rule.
    contextual = _apply_market_context(
        raw_daily,
        weekly=weekly,
        market=market,
        market_label=market_label,
        market_weak=market_weak,
    )
    no_trade_value = _analyzed_with_liquidity(
        analyzed,
        contextual,
        market=market,
        ignore_trade_value=True,
        ignore_volume=False,
    )
    slug = _family_slug("거래대금")
    result[f"ablation_{slug}_action"] = canonical_action(no_trade_value.final_action)
    result[f"ablation_{slug}_score"] = no_trade_value.context_adjusted_score
    result[f"ablation_{slug}_status"] = "exact_liquidity_neutralization"

    # News/event variants are exact because the production final-action gate
    # consumes only the immutable MarketIntelligence object.
    intelligence = analyzed.intelligence
    if intelligence is None:
        for family in ("시장·뉴스 점수", "뉴스", "글로벌 이벤트"):
            result[f"ablation_{_family_slug(family)}_status"] = "missing_pit_snapshots"
    else:
        no_environment = replace(analyzed, intelligence=None)
        slug = _family_slug("시장·뉴스 점수")
        result[f"ablation_{slug}_action"] = canonical_action(no_environment.final_action)
        result[f"ablation_{slug}_score"] = no_environment.context_adjusted_score
        no_news = _neutralized_intelligence(intelligence, news=True)
        news_variant = replace(analyzed, intelligence=no_news)
        slug = _family_slug("뉴스")
        result[f"ablation_{slug}_action"] = canonical_action(news_variant.final_action)
        result[f"ablation_{slug}_score"] = news_variant.context_adjusted_score
        no_events = _neutralized_intelligence(intelligence, event=True)
        event_variant = replace(analyzed, intelligence=no_events)
        slug = _family_slug("글로벌 이벤트")
        result[f"ablation_{slug}_action"] = canonical_action(event_variant.final_action)
        result[f"ablation_{slug}_score"] = event_variant.context_adjusted_score

    # The following families are structurally entangled with grade, blocks,
    # levels, or context.  Mark them non-identifiable until a production rule
    # decomposition is supplied; treating an arbitrary score subtraction as
    # an ablation would be misleading.
    for family in (
        "일목 구조",
    ):
        result[f"ablation_{_family_slug(family)}_status"] = "non_identifiable_without_rule_rewrite"
    return result


def _ablate_raw_reading(reading: IchimokuReading, disabled: str) -> IchimokuReading:
    bullish = reading.bullish_score
    bearish = reading.bearish_score
    blocks = list(reading.hard_blocks)
    risks = list(reading.risks)
    reasons = list(reading.reasons)
    if disabled == "adx_di":
        if reading.adx14 >= 25.0 and reading.plus_di14 > reading.minus_di14:
            bullish = max(0, bullish - 1)
        elif reading.adx14 >= 25.0 and reading.minus_di14 > reading.plus_di14:
            bearish = max(0, bearish - 1)
        risks = [item for item in risks if not item.startswith("ADX ")]
        reasons = [item for item in reasons if not item.startswith("ADX ")]
    elif disabled == "volume":
        if (
            reading.kumo_breakout == "상향 돌파"
            and reading.volume_ratio is not None
            and reading.volume_ratio >= 1.0
        ):
            bullish = max(0, bullish - 2)
        blocks = [item for item in blocks if "거래량 없는 첫 돌파" not in item]
        risks = [
            item
            for item in risks
            if "거래량 확인이 부족" not in item and "거래량이 붙었습니다" not in item
        ]
        reasons = [item for item in reasons if "거래량이 붙었습니다" not in item]
    elif disabled == "atr":
        blocks = [
            item
            for item in blocks
            if "너무 긴 캔들" not in item
            and "중기선과 거리가 너무" not in item
            and "기대 손익비가 부족" not in item
        ]
        risks = [
            item
            for item in risks
            if "ATR" not in item and "손익비" not in item
        ]
    else:
        raise ValueError(f"지원하지 않는 ablation: {disabled}")

    distance_atr = (
        abs(reading.close - reading.kijun) / reading.atr14 if reading.atr14 > 0 else 0.0
    )
    complete_bullish = (
        reading.price_position == "구름 위"
        and reading.tenkan > reading.kijun
        and reading.kijun_slope in {"상승", "수평"}
        and reading.chikou_state == "강세 확인"
        and reading.future_cloud == "양의 구름"
        and (disabled == "atr" or distance_atr <= 3.0)
    )
    if complete_bullish:
        grade = "A+"
    elif (
        reading.price_position == "구름 위"
        and bullish >= 5
        and reading.chikou_state != "약세"
    ):
        grade = "A"
    elif reading.price_position == "구름 위" or reading.tk_state == "최근 상향 교차":
        grade = "B"
    elif reading.price_position == "구름 안":
        grade = "C"
    else:
        grade = "D"

    confidence_points = sum(
        (
            reading.price_position == "구름 위",
            reading.tenkan > reading.kijun,
            reading.kijun_slope in {"상승", "수평"},
            reading.chikou_state == "강세 확인",
            reading.future_cloud == "양의 구름",
            disabled != "volume"
            and reading.volume_ratio is not None
            and reading.volume_ratio >= 1.0,
        )
    )
    confidence = (
        "높음"
        if confidence_points >= 5 and not blocks
        else "보통" if confidence_points >= 3 else "낮음"
    )
    if grade == "A+" and not blocks:
        action = "관심 후보 - 현재가 추격보다 지지 확인 후 판단"
    elif grade in {"A+", "A", "B"}:
        action = "기다림 - 아래 확인 조건이 충족될 때만 다시 판단"
    else:
        action = "피하기 - 상승 구조가 확인될 때까지 신규 접근 보류"
    return replace(
        reading,
        grade=grade,
        confidence=confidence,
        bullish_score=bullish,
        bearish_score=bearish,
        risk_score=len(risks) + len(blocks) * 2,
        hard_blocks=tuple(dict.fromkeys(blocks)),
        risks=tuple(dict.fromkeys(risks)),
        reasons=tuple(dict.fromkeys(reasons)),
        action=action,
    )


def _analyzed_with_liquidity(
    template: AnalyzedStock,
    reading: IchimokuReading,
    *,
    market: str,
    ignore_trade_value: bool,
    ignore_volume: bool,
) -> AnalyzedStock:
    volume = reading.avg_volume_20
    value = reading.avg_trade_value_20
    if market.upper() == "US":
        min_volume, min_value = US_MIN_AVG_VOLUME, US_MIN_AVG_DOLLAR_VOLUME
    else:
        min_volume, min_value = KR_MIN_AVG_VOLUME, KR_MIN_AVG_TRADE_VALUE
    if ignore_trade_value and ignore_volume:
        liquid, reason = True, ""
    elif ignore_trade_value:
        liquid = volume is not None and volume >= min_volume
        reason = "" if liquid else (
            f"최근 20일 평균 거래량 {(volume or 0):,.0f}주 (기준 {min_volume:,.0f}주 이상)"
        )
    elif ignore_volume:
        liquid = value is not None and value >= min_value
        reason = "" if liquid else (
            f"최근 20일 평균 거래대금 {value or 0:,.0f} (기준 {min_value:,.0f} 이상)"
        )
    else:
        liquid, reason = _liquidity(reading, market)
    final_reading = reading if liquid else _apply_liquidity_block(reading, reason, market)
    return replace(
        template,
        daily=final_reading,
        liquid=liquid,
        liquidity_reason=reason,
    )


def _neutralized_intelligence(
    value: MarketIntelligence, *, news: bool = False, event: bool = False
) -> MarketIntelligence:
    news_score = 50 if news else value.news_score
    event_risk = 15 if event else value.event_risk
    score, label = _combine_environment_score(value.market_score, news_score, event_risk)
    return replace(
        value,
        score=score,
        label=label,
        news_score=news_score,
        event_risk=event_risk,
        headlines=() if news else value.headlines,
        event_headlines=() if event else value.event_headlines,
    )


def _family_slug(value: str) -> str:
    mapping = {
        "일목 구조": "ichimoku",
        "ADX / DI": "adx_di",
        "ATR": "atr",
        "거래량": "volume",
        "거래대금": "trade_value",
        "주봉 확인": "weekly",
        "60분봉": "intraday_60m",
        "과거 유사패턴": "similarity",
        "시장 방향": "market_direction",
        "시장·뉴스 점수": "environment",
        "뉴스": "news",
        "글로벌 이벤트": "global_event",
    }
    return mapping[value]


def _forward_outcomes(
    signal: Mapping[str, object],
    *,
    frame: pd.DataFrame,
    signal_frame: pd.DataFrame | None = None,
    position: int,
    horizons: Iterable[int],
    invalidation_price: float,
    large_loss_threshold_pct: float,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    signal_prices = signal_frame if signal_frame is not None else frame
    if not signal_prices.index.equals(frame.index):
        raise ValueError("신호 가격과 성과 가격의 거래일 인덱스가 다릅니다.")
    entry = float(frame.iloc[position + 1]["open"])
    for horizon in horizons:
        exit_position = position + int(horizon)
        if exit_position >= len(frame):
            continue
        path = frame.iloc[position + 1 : exit_position + 1]
        signal_path = signal_prices.iloc[position + 1 : exit_position + 1]
        exit_price = float(frame.iloc[exit_position]["close"])
        forward_return = (exit_price / entry - 1.0) * 100.0
        mae = (float(path["low"].min()) / entry - 1.0) * 100.0
        mfe = (float(path["high"].max()) / entry - 1.0) * 100.0
        close_path = np.r_[entry, path["close"].astype(float).to_numpy()]
        running_peak = np.maximum.accumulate(close_path)
        max_drawdown = float(np.min((close_path / running_peak - 1.0) * 100.0))
        row = dict(signal)
        row.update(
            {
                "horizon": int(horizon),
                "entry_price": entry,
                "exit_price": exit_price,
                "forward_return_pct": forward_return,
                "mae_pct": mae,
                "mfe_pct": mfe,
                "max_drawdown_pct": max_drawdown,
                "invalidation_hit": bool((signal_path["low"] <= invalidation_price).any()),
                "large_loss": forward_return <= large_loss_threshold_pct,
            }
        )
        rows.append(row)
    return rows


def aggregate_performance(frame: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    columns = [
        *group_columns,
        "horizon",
        "sample_count",
        "unique_dates",
        "mean_return_pct",
        "median_return_pct",
        "up_rate_pct",
        "mean_mae_pct",
        "mean_mfe_pct",
        "mean_max_drawdown_pct",
        "invalidation_rate_pct",
        "large_loss_rate_pct",
        "return_volatility_pct",
        "return_to_volatility",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, object]] = []
    groups = [*group_columns, "horizon"]
    grouped = frame.groupby(groups[0] if len(groups) == 1 else groups, dropna=False)
    for keys, sample in grouped:
        key_values = keys if isinstance(keys, tuple) else (keys,)
        record = dict(zip(groups, key_values))
        returns = sample["forward_return_pct"].astype(float)
        volatility = float(returns.std(ddof=0))
        record.update(
            {
                "sample_count": int(len(sample)),
                "unique_dates": int(sample["as_of"].nunique()),
                "mean_return_pct": float(returns.mean()),
                "median_return_pct": float(returns.median()),
                "up_rate_pct": float((returns > 0).mean() * 100.0),
                "mean_mae_pct": float(sample["mae_pct"].mean()),
                "mean_mfe_pct": float(sample["mfe_pct"].mean()),
                "mean_max_drawdown_pct": float(sample["max_drawdown_pct"].mean()),
                "invalidation_rate_pct": float(sample["invalidation_hit"].mean() * 100.0),
                "large_loss_rate_pct": float(sample["large_loss"].mean() * 100.0),
                "return_volatility_pct": volatility,
                "return_to_volatility": (
                    float(returns.mean()) / volatility if volatility > 0 else np.nan
                ),
            }
        )
        rows.append(record)
    return pd.DataFrame(rows, columns=columns)


def ranking_performance(
    outcomes: pd.DataFrame, *, min_cross_section: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    ic_rows: list[dict[str, object]] = []
    group_frames: list[pd.DataFrame] = []
    if outcomes.empty:
        return pd.DataFrame(), pd.DataFrame()
    for horizon in RANKING_HORIZONS:
        subset = outcomes.loc[outcomes["horizon"] == horizon].copy()
        for score_name in ("technical_score", "context_adjusted_score"):
            labeled: list[pd.DataFrame] = []
            for as_of, cross_section in subset.groupby("as_of"):
                usable = cross_section.dropna(subset=[score_name, "forward_return_pct"]).copy()
                if len(usable) < min_cross_section or usable[score_name].nunique() < 2:
                    continue
                ic_rows.append(
                    {
                        "as_of": as_of,
                        "horizon": horizon,
                        "score": score_name,
                        "sample_count": len(usable),
                        "spearman": float(
                            usable[score_name].rank().corr(
                                usable["forward_return_pct"].rank()
                            )
                        ),
                    }
                )
                percentile = usable[score_name].rank(
                    ascending=False, method="average", pct=True
                )
                definitions = {
                    "Top 5%": percentile <= 0.05,
                    "Top 10%": percentile <= 0.10,
                    "Top 20%": percentile <= 0.20,
                    "Bottom 20%": percentile > 0.80,
                }
                for label, mask in definitions.items():
                    selected = usable.loc[mask].copy()
                    if selected.empty:
                        continue
                    selected["rank_group"] = label
                    selected["ranking_score"] = score_name
                    labeled.append(selected)
            if labeled:
                group_frames.append(pd.concat(labeled, ignore_index=True))
    ic = pd.DataFrame(ic_rows)
    if ic.empty:
        ic_summary = pd.DataFrame(
            columns=["horizon", "score", "date_count", "mean_spearman", "median_spearman"]
        )
    else:
        ic_summary = (
            ic.groupby(["horizon", "score"], as_index=False)
            .agg(
                date_count=("as_of", "nunique"),
                mean_spearman=("spearman", "mean"),
                median_spearman=("spearman", "median"),
            )
        )
    groups = (
        aggregate_performance(pd.concat(group_frames, ignore_index=True), ["ranking_score", "rank_group"])
        if group_frames
        else pd.DataFrame()
    )
    return ic_summary, groups


def action_ordering_test(action_results: pd.DataFrame) -> pd.DataFrame:
    """Test the predeclared 관심 > 기다림 > 피하기 ordering."""

    rows: list[dict[str, object]] = []
    if action_results.empty:
        return pd.DataFrame()
    for horizon, sample in action_results.groupby("horizon"):
        indexed = sample.set_index("action")
        missing = [action for action in ACTIONS if action not in indexed.index]
        if missing:
            rows.append(
                {
                    "horizon": horizon,
                    "status": "not_evaluable",
                    "missing_actions": ",".join(missing),
                    "mean_return_order_pass": False,
                    "median_return_order_pass": False,
                    "up_rate_order_pass": False,
                }
            )
            continue
        mean_values = [float(indexed.loc[action, "mean_return_pct"]) for action in ACTIONS]
        median_values = [float(indexed.loc[action, "median_return_pct"]) for action in ACTIONS]
        up_values = [float(indexed.loc[action, "up_rate_pct"]) for action in ACTIONS]
        rows.append(
            {
                "horizon": horizon,
                "status": "evaluated",
                "missing_actions": "",
                "mean_return_order_pass": mean_values[0] > mean_values[1] > mean_values[2],
                "median_return_order_pass": median_values[0] > median_values[1] > median_values[2],
                "up_rate_order_pass": up_values[0] > up_values[1] > up_values[2],
                "interest_mean_return_pct": mean_values[0],
                "wait_mean_return_pct": mean_values[1],
                "avoid_mean_return_pct": mean_values[2],
            }
        )
    return pd.DataFrame(rows)


def apply_walk_forward_splits(
    signals: pd.DataFrame,
    outcomes: pd.DataFrame,
    *,
    purge_sessions: int,
    min_split_dates: int,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    dates = sorted(signals["as_of"].unique())
    usable = len(dates) - 2 * purge_sessions
    if usable < min_split_dates * 3:
        signals = signals.copy()
        outcomes = outcomes.copy()
        signals["split"] = "insufficient"
        outcomes["split"] = "insufficient"
        return signals, outcomes, {
            "status": "insufficient_dates",
            "date_count": len(dates),
            "required_date_count": min_split_dates * 3 + 2 * purge_sessions,
            "purge_sessions": purge_sessions,
            "test_used_for_selection": False,
        }
    train_count = max(min_split_dates, int(usable * 0.60))
    validation_count = max(min_split_dates, int(usable * 0.20))
    test_count = usable - train_count - validation_count
    if validation_count < min_split_dates or test_count < min_split_dates:
        validation_count = min_split_dates
        test_count = min_split_dates
        train_count = usable - validation_count - test_count
    train_dates = dates[:train_count]
    validation_start = train_count + purge_sessions
    validation_dates = dates[validation_start : validation_start + validation_count]
    test_start = validation_start + validation_count + purge_sessions
    test_dates = dates[test_start : test_start + test_count]
    mapping = {item: "train" for item in train_dates}
    mapping.update({item: "validation" for item in validation_dates})
    mapping.update({item: "test" for item in test_dates})
    signals = signals.copy()
    outcomes = outcomes.copy()
    signals["split"] = signals["as_of"].map(mapping).fillna("purged")
    outcomes["split"] = outcomes["as_of"].map(mapping).fillna("purged")
    return signals, outcomes, {
        "status": "applied",
        "date_count": len(dates),
        "purge_sessions": purge_sessions,
        "train": _date_range(train_dates),
        "validation": _date_range(validation_dates),
        "test": _date_range(test_dates),
        "test_used_for_selection": False,
    }


def _date_range(values: list[str]) -> dict[str, object]:
    return {
        "start": values[0] if values else None,
        "end": values[-1] if values else None,
        "date_count": len(values),
    }


def signal_correlation_table(signals: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "price_above_cloud",
        "tenkan_above_kijun",
        "kijun_nonfalling",
        "chikou_strong",
        "future_cloud_bullish",
        "adx_di_bullish",
        "volume_confirmed",
        "weekly_bullish",
        "market_bullish",
        "atr_extension_block",
    ]
    available = [item for item in columns if item in signals]
    rows: list[dict[str, object]] = []
    for left_index, left in enumerate(available):
        for right in available[left_index + 1 :]:
            pair = signals[[left, right]].dropna().astype(float)
            correlation = (
                pair[left].corr(pair[right])
                if len(pair) >= 3
                and pair[left].nunique() >= 2
                and pair[right].nunique() >= 2
                else np.nan
            )
            rows.append(
                {
                    "category": "decision_signal",
                    "signal_a": left,
                    "signal_b": right,
                    "sample_count": len(pair),
                    "phi_correlation": correlation,
                    "absolute_correlation": abs(correlation) if pd.notna(correlation) else np.nan,
                }
            )
    similarity_features = [
        f"similarity_feature_{name}"
        for name in FEATURES
        if f"similarity_feature_{name}" in signals
    ]
    for left_index, left in enumerate(similarity_features):
        for right in similarity_features[left_index + 1 :]:
            pair = signals[[left, right]].dropna().astype(float)
            correlation = (
                pair[left].corr(pair[right])
                if len(pair) >= 20
                and pair[left].nunique() >= 2
                and pair[right].nunique() >= 2
                else np.nan
            )
            rows.append(
                {
                    "category": "similarity_feature",
                    "signal_a": left.removeprefix("similarity_feature_"),
                    "signal_b": right.removeprefix("similarity_feature_"),
                    "sample_count": len(pair),
                    "phi_correlation": correlation,
                    "absolute_correlation": abs(correlation) if pd.notna(correlation) else np.nan,
                }
            )
    return pd.DataFrame(rows).sort_values(
        "absolute_correlation", ascending=False, na_position="last"
    ) if rows else pd.DataFrame()


def similarity_performance(outcomes: pd.DataFrame) -> pd.DataFrame:
    if outcomes.empty:
        return pd.DataFrame()
    sample = outcomes.loc[outcomes["horizon"] == 10].copy()
    sample = sample.loc[sample["similar_sample_count"] > 0]
    if sample.empty:
        return pd.DataFrame()
    rows: list[dict[str, object]] = []
    rows.extend(_similarity_group_rows(sample, "sample_count", pd.cut(
        sample["similar_sample_count"], [-1, 14, 24, np.inf], labels=["<15", "15-24", "25+"]
    )))
    rows.extend(_similarity_quantile_rows(sample, "distance", "similar_nearest_distance"))
    rows.extend(_similarity_quantile_rows(sample, "predicted_up_rate", "similar_up_rate"))
    rows.extend(_similarity_group_rows(sample, "market_context", sample["market_context"]))
    predicted = sample.dropna(subset=["similar_up_rate"])
    if not predicted.empty:
        probability = predicted["similar_up_rate"].astype(float) / 100.0
        actual = (predicted["forward_return_pct"] > 0).astype(float)
        rows.append(
            {
                "test": "overall",
                "group": "all",
                "sample_count": len(predicted),
                "mean_return_pct": predicted["forward_return_pct"].mean(),
                "up_rate_pct": actual.mean() * 100.0,
                "brier_score": float(((probability - actual) ** 2).mean()),
                "spearman_with_return": _spearman(
                    predicted["similar_up_rate"], predicted["forward_return_pct"]
                ),
                "sample_count_vs_abs_error_spearman": _spearman(
                    predicted["similar_sample_count"], (probability - actual).abs()
                ),
                "distance_vs_return_spearman": _spearman(
                    predicted["similar_nearest_distance"], predicted["forward_return_pct"]
                ),
            }
        )
    return pd.DataFrame(rows)


def _similarity_group_rows(
    frame: pd.DataFrame, test: str, labels: pd.Series
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    working = frame.copy()
    working["_group"] = labels
    for label, sample in working.groupby("_group", observed=True, dropna=False):
        predicted = sample.dropna(subset=["similar_up_rate"])
        if predicted.empty:
            brier = np.nan
            calibration_error = np.nan
        else:
            probability = predicted["similar_up_rate"].astype(float) / 100.0
            actual = (predicted["forward_return_pct"] > 0).astype(float)
            brier = float(((probability - actual) ** 2).mean())
            calibration_error = float(abs(probability.mean() - actual.mean()) * 100.0)
        rows.append(
            {
                "test": test,
                "group": str(label),
                "sample_count": len(sample),
                "mean_return_pct": sample["forward_return_pct"].mean(),
                "median_return_pct": sample["forward_return_pct"].median(),
                "up_rate_pct": (sample["forward_return_pct"] > 0).mean() * 100.0,
                "mean_distance": sample["similar_nearest_distance"].mean(),
                "mean_predicted_up_rate": sample["similar_up_rate"].mean(),
                "calibration_abs_error_pct": calibration_error,
                "brier_score": brier,
            }
        )
    return rows


def _similarity_quantile_rows(
    frame: pd.DataFrame, test: str, column: str
) -> list[dict[str, object]]:
    usable = frame.dropna(subset=[column]).copy()
    if len(usable) < 8 or usable[column].nunique() < 4:
        return []
    try:
        labels = pd.qcut(usable[column], 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop")
    except ValueError:
        return []
    return _similarity_group_rows(usable, test, labels)


def _spearman(left: pd.Series, right: pd.Series) -> float:
    pair = pd.concat([left, right], axis=1).dropna()
    if len(pair) < 3 or pair.iloc[:, 0].nunique() < 2 or pair.iloc[:, 1].nunique() < 2:
        return float("nan")
    return float(pair.iloc[:, 0].rank().corr(pair.iloc[:, 1].rank()))


def ablation_performance(outcomes: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "family",
        "status",
        "split",
        "horizon",
        "baseline_interest_count",
        "ablated_interest_count",
        "baseline_mean_return_pct",
        "ablated_mean_return_pct",
        "mean_return_delta_pct",
        "baseline_return_to_volatility",
        "ablated_return_to_volatility",
        "return_to_volatility_delta",
        "recommendation",
    ]
    if outcomes.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, object]] = []
    splits = ["all", *[item for item in ("train", "validation", "test") if item in set(outcomes["split"])]]
    for family in ABLATION_FAMILIES:
        slug = _family_slug(family)
        action_column = f"ablation_{slug}_action"
        status_column = f"ablation_{slug}_status"
        statuses = set(outcomes[status_column].dropna().astype(str))
        family_status = ",".join(sorted(statuses)) if statuses else "unavailable"
        for split in splits:
            split_frame = outcomes if split == "all" else outcomes.loc[outcomes["split"] == split]
            for horizon in sorted(split_frame["horizon"].unique()):
                sample = split_frame.loc[split_frame["horizon"] == horizon]
                baseline = sample.loc[sample["action"] == "관심"]
                variant = sample.loc[sample[action_column] == "관심"]
                base_mean, base_rtv = _mean_and_rtv(baseline)
                variant_mean, variant_rtv = _mean_and_rtv(variant)
                identifiable = family_status not in {
                    "non_identifiable_without_rule_rewrite",
                    "missing_pit_snapshots",
                }
                rows.append(
                    {
                        "family": family,
                        "status": family_status,
                        "split": split,
                        "horizon": horizon,
                        "baseline_interest_count": len(baseline),
                        "ablated_interest_count": len(variant),
                        "baseline_mean_return_pct": base_mean,
                        "ablated_mean_return_pct": variant_mean if identifiable else np.nan,
                        "mean_return_delta_pct": (
                            variant_mean - base_mean
                            if identifiable and pd.notna(base_mean) and pd.notna(variant_mean)
                            else np.nan
                        ),
                        "baseline_return_to_volatility": base_rtv,
                        "ablated_return_to_volatility": variant_rtv if identifiable else np.nan,
                        "return_to_volatility_delta": (
                            variant_rtv - base_rtv
                            if identifiable and pd.notna(base_rtv) and pd.notna(variant_rtv)
                            else np.nan
                        ),
                        "recommendation": "판정 보류",
                    }
                )
    return pd.DataFrame(rows, columns=columns)


def _mean_and_rtv(frame: pd.DataFrame) -> tuple[float, float]:
    if frame.empty:
        return float("nan"), float("nan")
    returns = frame["forward_return_pct"].astype(float)
    volatility = float(returns.std(ddof=0))
    return float(returns.mean()), float(returns.mean() / volatility) if volatility > 0 else float("nan")


def _metadata(
    *,
    stocks: Mapping[str, tuple[StockInfo, pd.DataFrame]],
    proxies: Mapping[str, pd.DataFrame],
    config: BacktestConfig,
    signals: pd.DataFrame,
    outcomes: pd.DataFrame,
    intelligence: Mapping[tuple[str, str], MarketIntelligence],
    universe: Mapping[str, set[str]],
    errors: list[dict[str, str]],
) -> dict[str, object]:
    ranges: dict[str, dict[str, object]] = {}
    for key, (stock, frame) in stocks.items():
        if frame.empty:
            continue
        ranges[stock.symbol or key] = {
            "start": pd.Timestamp(frame.index.min()).date().isoformat(),
            "end": pd.Timestamp(frame.index.max()).date().isoformat(),
            "rows": len(frame),
        }
    required_proxies = {"SPY", "QQQ"} if config.market.upper() == "US" else {"KOSPI", "KOSDAQ"}
    intelligence_complete = (
        not signals.empty
        and "environment_score" in signals
        and bool(signals["environment_score"].notna().all())
    )
    proxy_complete = required_proxies.issubset(proxies)
    intelligence_required = config.baseline_mode == "FULL_CONTEXT_BASELINE"
    data_requirements = {
        "price_history": bool(stocks) and bool(signals.size),
        "market_proxies": proxy_complete,
        "universe_snapshots": bool(universe),
        "historical_universe_verified": config.dataset_point_in_time_verified,
        "delisted_securities_included": config.delisted_securities_included,
        "corporate_action_verified": config.price_policy_verified,
        "exchange_calendar_verified": config.exchange_calendar_verified,
        "intelligence_complete": intelligence_complete or not intelligence_required,
        "independent_dates_sufficient": False,
    }
    limitations: list[str] = []
    if not stocks:
        limitations.append("평가 대상 일반주식 일봉 캐시가 없습니다.")
    if not proxy_complete:
        limitations.append("시장 프록시의 point-in-time 이력이 없습니다.")
    if intelligence_required and not intelligence_complete:
        limitations.append("FULL_CONTEXT_BASELINE에 필요한 뉴스·글로벌 이벤트·환경점수의 과거 스냅샷이 없습니다.")
    elif not intelligence_complete:
        limitations.append("뉴스 스냅샷이 없어 FULL_CONTEXT_BASELINE은 불가능하며 TECHNICAL_BASELINE만 평가합니다.")
    if not universe:
        limitations.append("날짜별 과거 유니버스 스냅샷이 없어 survivorship bias를 제거할 수 없습니다.")
    if universe and not config.dataset_point_in_time_verified:
        limitations.append("universe 파일은 있으나 공급자/manifest에서 역사적 point-in-time 완전성이 검증되지 않았습니다.")
    if not config.price_policy_verified:
        limitations.append("수정주가·기업행사 정책이 검증되지 않아 분할/상장폐지 구간의 수익률을 신뢰할 수 없습니다.")
    if not config.delisted_securities_included:
        limitations.append("상장폐지 종목과 상장폐지 수익이 포함되지 않아 생존편향을 통제할 수 없습니다.")
    if not config.exchange_calendar_verified:
        limitations.append("버전이 고정된 실제 거래소 달력이 dataset에 적용됐는지 검증되지 않았습니다.")
    limitations.append("가격의 수정주가·기업행사 처리 정책과 원천 데이터의 point-in-time 보존 여부는 입력 제공자가 검증해야 합니다.")
    proxy_ranges = {
        symbol: {
            "start": pd.Timestamp(frame.index.min()).date().isoformat(),
            "end": pd.Timestamp(frame.index.max()).date().isoformat(),
            "rows": len(frame),
        }
        for symbol, frame in proxies.items()
        if not frame.empty
    }
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": asdict(config),
        "stock_count": len(stocks),
        "signal_count": len(signals),
        "outcome_count": len(outcomes),
        "stock_ranges": ranges,
        "proxy_symbols": sorted(proxies),
        "proxy_ranges": proxy_ranges,
        "intelligence_snapshot_count": len(intelligence),
        "universe_snapshot_count": len(universe),
        "trusted_baseline_available": False,
        "trust_level": "NOT_EVALUABLE" if not len(signals) else "EXPLORATORY",
        "data_requirements": data_requirements,
        "status": "evaluated" if len(signals) else "not_evaluable",
        "limitations": limitations,
        "errors": errors[:500],
        "entry_definition": "다음 거래일 시가",
        "exit_definition": "Forward N번째 거래일 종가",
        "max_drawdown_definition": "진입가와 일별 종가 경로의 최대 낙폭",
        "mae_mfe_definition": "진입 후 N거래일 내 저가/고가 기준",
        "large_loss_threshold_pct": config.large_loss_threshold_pct,
        "test_used_for_threshold_or_weight_selection": False,
        "baseline_mode": config.baseline_mode,
        "dataset_point_in_time_verified": config.dataset_point_in_time_verified,
        "price_policy_verified": config.price_policy_verified,
        "delisted_securities_included": config.delisted_securities_included,
        "exchange_calendar_verified": config.exchange_calendar_verified,
    }


def _finalize_trust(metadata: dict[str, object], walk_forward: Mapping[str, object]) -> None:
    requirements = dict(metadata.get("data_requirements", {}))
    requirements["independent_dates_sufficient"] = walk_forward.get("status") == "applied"
    metadata["data_requirements"] = requirements
    if metadata.get("status") != "evaluated":
        level = "NOT_EVALUABLE"
    else:
        core = all(
            requirements.get(name, False)
            for name in (
                "price_history",
                "market_proxies",
                "universe_snapshots",
                "historical_universe_verified",
                "delisted_securities_included",
                "corporate_action_verified",
                "intelligence_complete",
            )
        )
        if not core:
            level = "EXPLORATORY"
        elif not requirements.get("exchange_calendar_verified") or not requirements.get(
            "independent_dates_sufficient"
        ):
            level = "PARTIALLY_TRUSTED"
        else:
            level = "TRUSTED"
    metadata["trust_level"] = level
    metadata["trusted_baseline_available"] = level == "TRUSTED"
    metadata["trust_blockers"] = [
        name for name, passed in requirements.items() if not passed
    ]


def _empty_result_tables(config: BacktestConfig) -> dict[str, pd.DataFrame]:
    status = "not_evaluable"
    baseline = pd.DataFrame(
        [{"horizon": horizon, "sample_count": 0, "status": status} for horizon in config.horizons]
    )
    grade_results = pd.DataFrame(
        [
            {"grade": grade, "horizon": horizon, "sample_count": 0, "status": status}
            for grade in GRADES
            for horizon in config.horizons
        ]
    )
    action_results = pd.DataFrame(
        [
            {"action": action, "horizon": horizon, "sample_count": 0, "status": status}
            for action in ACTIONS
            for horizon in config.horizons
        ]
    )
    action_ordering = pd.DataFrame(
        [
            {
                "horizon": horizon,
                "status": status,
                "mean_return_order_pass": False,
                "median_return_order_pass": False,
                "up_rate_order_pass": False,
            }
            for horizon in config.horizons
        ]
    )
    rank_ic = pd.DataFrame(
        [
            {"score": score, "horizon": horizon, "date_count": 0, "status": status}
            for score in ("technical_score", "context_adjusted_score")
            for horizon in RANKING_HORIZONS
        ]
    )
    ranking_groups = pd.DataFrame(
        [
            {
                "ranking_score": score,
                "rank_group": group,
                "horizon": horizon,
                "sample_count": 0,
                "status": status,
            }
            for score in ("technical_score", "context_adjusted_score")
            for group in ("Top 5%", "Top 10%", "Top 20%", "Bottom 20%")
            for horizon in RANKING_HORIZONS
        ]
    )
    walk_forward = pd.DataFrame(
        [
            {"split": split, "horizon": horizon, "sample_count": 0, "status": status}
            for split in ("train", "validation", "test")
            for horizon in config.horizons
        ]
    )
    ablation = pd.DataFrame(
        [
            {
                "family": family,
                "status": "no_eligible_stock_history",
                "split": split,
                "horizon": horizon,
                "baseline_interest_count": 0,
                "ablated_interest_count": 0,
                "recommendation": "판정 보류",
            }
            for family in ABLATION_FAMILIES
            for split in ("all", "validation", "test")
            for horizon in config.horizons
        ]
    )
    return {
        "baseline": baseline,
        "grade_results": grade_results,
        "action_results": action_results,
        "action_ordering": action_ordering,
        "rank_ic": rank_ic,
        "ranking_groups": ranking_groups,
        "walk_forward": walk_forward,
        "ablation": ablation,
        "signal_correlations": pd.DataFrame(
            columns=[
                "category",
                "signal_a",
                "signal_b",
                "sample_count",
                "phi_correlation",
                "absolute_correlation",
                "status",
            ]
        ),
        "similarity_validation": pd.DataFrame(
            [
                {"test": test, "group": "all", "sample_count": 0, "status": status}
                for test in ("sample_count", "distance", "predicted_up_rate", "market_context")
            ]
        ),
    }


def _signal_columns() -> list[str]:
    columns = [
        "market",
        "as_of",
        "symbol",
        "exchange",
        "name",
        "grade",
        "action",
        "action_text",
        "technical_score",
        "context_adjusted_score",
        "confidence",
        "bullish_score",
        "bearish_score",
        "risk_score",
        "hard_block_count",
        "liquid",
        "liquidity_reason",
        "market_context",
        "market_weak",
        "price_above_cloud",
        "tenkan_above_kijun",
        "kijun_nonfalling",
        "chikou_strong",
        "future_cloud_bullish",
        "adx_di_bullish",
        "volume_confirmed",
        "weekly_bullish",
        "market_bullish",
        "atr_extension_block",
        "similar_sample_count",
        "similar_up_rate",
        "similar_nearest_distance",
        "similar_status",
        "similar_match_level",
        "environment_score",
        "market_score",
        "news_score",
        "event_risk",
        "intelligence_generated_at",
    ]
    columns.extend(f"similarity_feature_{name}" for name in FEATURES)
    for family in ABLATION_FAMILIES:
        slug = _family_slug(family)
        columns.extend(
            (
                f"ablation_{slug}_action",
                f"ablation_{slug}_score",
                f"ablation_{slug}_status",
            )
        )
    return columns


def write_evaluation_bundle(bundle: EvaluationBundle, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metadata.json").write_text(
        json.dumps(bundle.metadata, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    tables = {
        "signals.csv": bundle.signals,
        "outcomes.csv": bundle.outcomes,
        "baseline.csv": bundle.baseline,
        "grade_results.csv": bundle.grade_results,
        "action_results.csv": bundle.action_results,
        "action_ordering.csv": bundle.action_ordering,
        "rank_ic.csv": bundle.rank_ic,
        "ranking_groups.csv": bundle.ranking_groups,
        "walk_forward.csv": bundle.walk_forward,
        "ablation.csv": bundle.ablation,
        "signal_correlations.csv": bundle.signal_correlations,
        "similarity_validation.csv": bundle.similarity_validation,
    }
    for name, frame in tables.items():
        frame.to_csv(output_dir / name, index=False, encoding="utf-8-sig")
    (output_dir / "report.md").write_text(_markdown_report(bundle), encoding="utf-8")


def _markdown_report(bundle: EvaluationBundle) -> str:
    meta = bundle.metadata
    status = meta.get("status", "unknown")
    trusted = meta.get("trusted_baseline_available", False)
    limitations = meta.get("limitations", [])
    return "\n".join(
        [
            "# 분석기 Point-in-time 평가 결과",
            "",
            f"- 상태: **{status}**",
            f"- 데이터 신뢰도: **{meta.get('trust_level', 'NOT_EVALUABLE')}**",
            f"- 신뢰 가능한 Baseline: **{'가능' if trusted else '불가능'}**",
            f"- 신호 수: {meta.get('signal_count', 0)}",
            f"- Outcome 수: {meta.get('outcome_count', 0)}",
            "- 진입: 다음 거래일 시가 / 청산: Forward N번째 거래일 종가",
            "- 최종 Test를 임계값·가중치 선택에 사용하지 않음",
            "",
            "## 데이터 한계",
            "",
            *[f"- {item}" for item in limitations],
            "",
            "## 결론",
            "",
            (
                "현재 데이터로는 분석기가 미래 상대성과를 구분하는지 판단할 수 없습니다. "
                "대상 종목, 날짜별 유니버스, 시장 프록시, 뉴스/이벤트의 과거 스냅샷이 필요합니다."
                if not trusted
                else "세부 성능은 CSV 산출물에서 확인하십시오."
            ),
            "",
        ]
    )


def _json_default(value: object) -> object:
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    raise TypeError(f"직렬화할 수 없는 값: {type(value)!r}")
