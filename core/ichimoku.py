from __future__ import annotations

"""Look-ahead-safe Ichimoku calculations and US-stock interpretation."""

import math
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pandas as pd


TENKAN_PERIOD = 9
KIJUN_PERIOD = 26
SPAN_B_PERIOD = 52
DISPLACEMENT = 26
MIN_BARS = SPAN_B_PERIOD + DISPLACEMENT + 2


@dataclass(frozen=True)
class IchimokuReading:
    timeframe: str
    data_timestamp: str
    source_range: str
    close: float
    tenkan: float
    kijun: float
    cloud_top: float
    cloud_bottom: float
    future_span_a: float
    future_span_b: float
    price_position: str
    tk_state: str
    kijun_slope: str
    chikou_state: str
    future_cloud: str
    kumo_breakout: str
    cloud_thickness_pct: float
    kijun_distance_pct: float
    volume_ratio: float | None
    avg_volume_20: float | None
    avg_trade_value_20: float | None
    candle_range_pct: float
    flat_span_b_levels: tuple[float, ...]
    volume_profile_levels: tuple[float, ...]
    grade: str
    confidence: str
    bullish_score: int
    bearish_score: int
    risk_score: int
    hard_blocks: tuple[str, ...]
    reasons: tuple[str, ...]
    risks: tuple[str, ...]
    watch_price: float
    invalidation_price: float
    first_target_price: float
    action: str
    higher_timeframe: str = "확인 전"
    market_context: str = "확인 전"


def prepare_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    required = ("open", "high", "low", "close", "volume")
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"OHLCV 열이 없습니다: {', '.join(missing)}")
    result = frame.copy()
    for column in required:
        result[column] = pd.to_numeric(result[column], errors="coerce")
    if "trade_value" not in result:
        result["trade_value"] = result["close"] * result["volume"]
    else:
        result["trade_value"] = pd.to_numeric(result["trade_value"], errors="coerce")
        result["trade_value"] = result["trade_value"].fillna(result["close"] * result["volume"])
    result = result.replace([np.inf, -np.inf], np.nan)
    result = result.dropna(subset=["open", "high", "low", "close"])
    result = result[(result[["open", "high", "low", "close"]] > 0).all(axis=1)]
    result = result[~result.index.duplicated(keep="last")].sort_index()
    if len(result) < MIN_BARS:
        raise ValueError(f"일목 분석에는 최소 {MIN_BARS}개 캔들이 필요합니다.")
    return result


def calculate_ichimoku(frame: pd.DataFrame) -> pd.DataFrame:
    result = prepare_ohlcv(frame)
    high9 = result["high"].rolling(TENKAN_PERIOD, min_periods=TENKAN_PERIOD).max()
    low9 = result["low"].rolling(TENKAN_PERIOD, min_periods=TENKAN_PERIOD).min()
    high26 = result["high"].rolling(KIJUN_PERIOD, min_periods=KIJUN_PERIOD).max()
    low26 = result["low"].rolling(KIJUN_PERIOD, min_periods=KIJUN_PERIOD).min()
    high52 = result["high"].rolling(SPAN_B_PERIOD, min_periods=SPAN_B_PERIOD).max()
    low52 = result["low"].rolling(SPAN_B_PERIOD, min_periods=SPAN_B_PERIOD).min()
    result["tenkan"] = (high9 + low9) / 2.0
    result["kijun"] = (high26 + low26) / 2.0
    result["span_a_raw"] = (result["tenkan"] + result["kijun"]) / 2.0
    result["span_b_raw"] = (high52 + low52) / 2.0
    result["cloud_a"] = result["span_a_raw"].shift(DISPLACEMENT)
    result["cloud_b"] = result["span_b_raw"].shift(DISPLACEMENT)
    result["cloud_top"] = result[["cloud_a", "cloud_b"]].max(axis=1)
    result["cloud_bottom"] = result[["cloud_a", "cloud_b"]].min(axis=1)
    result["volume_avg20_prior"] = result["volume"].shift(1).rolling(20, min_periods=10).mean()
    result["trade_value_avg20_prior"] = result["trade_value"].shift(1).rolling(20, min_periods=10).mean()
    result["volume_ratio"] = result["volume"] / result["volume_avg20_prior"].replace(0, np.nan)
    return result


def resample_weekly(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = prepare_ohlcv(frame)
    aggregation: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "trade_value": "sum",
    }
    return prepared.resample("W-FRI").agg(aggregation).dropna(subset=["open", "high", "low", "close"])


def analyze_ichimoku(frame: pd.DataFrame, *, timeframe: str = "일봉") -> IchimokuReading:
    ind = calculate_ichimoku(frame)
    row = ind.iloc[-1]
    previous = ind.iloc[-2]
    values = [row.get(name) for name in ("tenkan", "kijun", "cloud_top", "cloud_bottom", "span_a_raw", "span_b_raw")]
    if not all(_finite(value) for value in values):
        raise ValueError("현재 캔들에서 일목균형표 값을 완성하지 못했습니다.")

    close = float(row["close"])
    tenkan = float(row["tenkan"])
    kijun = float(row["kijun"])
    cloud_top = float(row["cloud_top"])
    cloud_bottom = float(row["cloud_bottom"])
    future_a = float(row["span_a_raw"])
    future_b = float(row["span_b_raw"])
    price_position = "구름 위" if close > cloud_top else "구름 아래" if close < cloud_bottom else "구름 안"

    tk_diff = ind["tenkan"] - ind["kijun"]
    recent_cross = _recent_cross(tk_diff, lookback=5)
    if recent_cross == "상향 교차":
        tk_state = "최근 상향 교차"
    elif recent_cross == "하향 교차":
        tk_state = "최근 하향 교차"
    else:
        tk_state = "전환선 우위" if tenkan > kijun else "기준선 우위" if tenkan < kijun else "동일"

    kijun_past = _past_finite(ind["kijun"], 5)
    slope_pct = ((kijun / kijun_past) - 1.0) * 100.0 if kijun_past and kijun_past > 0 else 0.0
    kijun_slope = "상승" if slope_pct > 0.15 else "하락" if slope_pct < -0.15 else "수평"

    past_index = len(ind) - 1 - DISPLACEMENT
    past = ind.iloc[past_index]
    past_high = float(past["high"])
    past_close = float(past["close"])
    past_cloud_top = _as_float(past.get("cloud_top"))
    past_cloud_bottom = _as_float(past.get("cloud_bottom"))
    if close > past_high and (past_cloud_top is None or close > past_cloud_top):
        chikou = "강세 확인"
    elif close > past_close:
        chikou = "부분 확인"
    elif close < past_close and (past_cloud_bottom is None or close < past_cloud_bottom):
        chikou = "약세"
    else:
        chikou = "혼조"

    future_cloud = "양의 구름" if future_a > future_b else "음의 구름" if future_a < future_b else "중립"
    prev_top = _as_float(previous.get("cloud_top"))
    prev_bottom = _as_float(previous.get("cloud_bottom"))
    if prev_top is not None and float(previous["close"]) <= prev_top and close > cloud_top:
        breakout = "상향 돌파"
    elif prev_bottom is not None and float(previous["close"]) >= prev_bottom and close < cloud_bottom:
        breakout = "하향 이탈"
    elif close > cloud_top and float(previous["close"]) > (prev_top or cloud_top):
        breakout = "구름 위 안착"
    elif close < cloud_bottom and float(previous["close"]) < (prev_bottom or cloud_bottom):
        breakout = "구름 아래 지속"
    else:
        breakout = "돌파 없음"

    volume_ratio = _as_float(row.get("volume_ratio"))
    avg_volume = _as_float(row.get("volume_avg20_prior"))
    avg_trade_value = _as_float(row.get("trade_value_avg20_prior"))
    candle_range_pct = max(0.0, (float(row["high"]) - float(row["low"])) / close * 100.0)
    cloud_thickness = max(0.0, (cloud_top - cloud_bottom) / close * 100.0)
    kijun_distance = (close / kijun - 1.0) * 100.0 if kijun > 0 else 0.0
    crosses = _cross_count(tk_diff.tail(15))

    bullish = 0
    bearish = 0
    risks: list[str] = []
    hard_blocks: list[str] = []
    reasons: list[str] = []
    if price_position == "구름 위":
        bullish += 2
        reasons.append("주가가 구름 위에 있어 큰 흐름이 상승 쪽입니다.")
    elif price_position == "구름 아래":
        bearish += 2
        hard_blocks.append("주가가 구름 아래라 아직 하락 구조입니다.")
    else:
        hard_blocks.append("주가가 구름 안이라 방향이 확정되지 않았습니다.")
        risks.append("횡보 구간의 가짜 신호 가능성이 큽니다.")
    if tenkan > kijun:
        bullish += 1
        reasons.append("단기선이 중기선 위에 있습니다.")
    elif tenkan < kijun:
        bearish += 1
        risks.append("단기선이 중기선 아래에 있습니다.")
    if kijun_slope in {"상승", "수평"}:
        bullish += 1
    else:
        bearish += 1
        risks.append("중기 기준선이 내려가고 있습니다.")
    if chikou == "강세 확인":
        bullish += 2
        reasons.append("현재 주가가 26기간 전 가격 장애물을 넘었습니다.")
    elif chikou == "약세":
        bearish += 2
        hard_blocks.append("현재 주가가 26기간 전 가격보다 약합니다.")
    elif chikou != "부분 확인":
        risks.append("26기간 전 가격 장애물을 완전히 넘지 못했습니다.")
    if future_cloud == "양의 구름":
        bullish += 1
    elif future_cloud == "음의 구름":
        bearish += 1
        risks.append("앞으로 표시되는 구름 모양이 아직 약세입니다.")
    if breakout == "상향 돌파":
        if volume_ratio is not None and volume_ratio >= 1.0:
            bullish += 2
            reasons.append(f"구름 돌파에 평소 대비 {volume_ratio:.2f}배 거래량이 붙었습니다.")
        else:
            risks.append("구름을 넘었지만 거래량 확인이 부족합니다.")
            hard_blocks.append("거래량 없는 첫 돌파는 다음 종가 확인이 필요합니다.")
    if candle_range_pct > 6.0:
        risks.append(f"오늘 캔들 폭이 {candle_range_pct:.1f}%로 커서 추격 위험이 있습니다.")
        hard_blocks.append("너무 긴 캔들 뒤 추격 진입을 보류합니다.")
    if abs(kijun_distance) > 10.0 and close > kijun:
        risks.append(f"주가가 중기선보다 {kijun_distance:.1f}% 높아 눌림 위험이 있습니다.")
        hard_blocks.append("중기선과 거리가 너무 멉니다.")
    if crosses >= 3:
        risks.append("단기선과 중기선이 최근 자주 엇갈려 횡보 가능성이 있습니다.")
    if cloud_thickness < 0.7:
        risks.append("구름이 얇아 방향이 쉽게 뒤집힐 수 있습니다.")

    risk_score = len(risks) + len(hard_blocks) * 2
    complete_bullish = (
        price_position == "구름 위"
        and tenkan > kijun
        and kijun_slope in {"상승", "수평"}
        and chikou == "강세 확인"
        and future_cloud == "양의 구름"
        and abs(kijun_distance) <= 10.0
    )
    if complete_bullish:
        grade = "A+"
    elif price_position == "구름 위" and bullish >= 5 and chikou != "약세":
        grade = "A"
    elif price_position == "구름 위" or recent_cross == "상향 교차":
        grade = "B"
    elif price_position == "구름 안":
        grade = "C"
    else:
        grade = "D"

    confidence_points = sum(
        (
            price_position == "구름 위",
            tenkan > kijun,
            kijun_slope in {"상승", "수평"},
            chikou == "강세 확인",
            future_cloud == "양의 구름",
            volume_ratio is not None and volume_ratio >= 1.0,
        )
    )
    confidence = "높음" if confidence_points >= 5 and not hard_blocks else "보통" if confidence_points >= 3 else "낮음"

    flat_levels = _flat_span_b_levels(ind["cloud_b"], close)
    volume_levels = _volume_profile_levels(ind, close)
    overlaps = [
        flat
        for flat in flat_levels
        if any(abs(flat - volume) / close <= 0.015 for volume in volume_levels)
    ]
    if overlaps:
        reasons.append(
            "선행 스팬 B 수평 가격과 과거 거래량 집중 가격이 겹쳐 중요한 지지·저항 후보가 있습니다."
        )
    supports = [
        value
        for value in (tenkan, kijun, cloud_top, cloud_bottom, *flat_levels, *volume_levels)
        if value < close
    ]
    resistance_candidates = [
        value
        for value in (*flat_levels, *volume_levels, float(ind["high"].tail(52).max()))
        if value > close * 1.005
    ]
    watch_price = max(value for value in (kijun, cloud_top) if _finite(value))
    nearest_support = max(supports) if supports else close * 0.94
    invalidation = nearest_support * 0.99
    risk_per_share = max(close - invalidation, close * 0.025)
    first_target = min(resistance_candidates) if resistance_candidates else close + risk_per_share * 2.0
    if first_target <= close * 1.02:
        first_target = close + risk_per_share * 2.0

    if grade == "A+" and not hard_blocks:
        action = "관심 후보 - 현재가 추격보다 지지 확인 후 판단"
    elif grade in {"A+", "A", "B"}:
        action = "기다림 - 아래 확인 조건이 충족될 때만 다시 판단"
    else:
        action = "피하기 - 상승 구조가 확인될 때까지 신규 접근 보류"

    return IchimokuReading(
        timeframe=timeframe,
        data_timestamp=_timestamp(ind.index[-1]),
        source_range=f"{_timestamp(ind.index[0])} ~ {_timestamp(ind.index[-1])} ({len(ind)}개)",
        close=close,
        tenkan=tenkan,
        kijun=kijun,
        cloud_top=cloud_top,
        cloud_bottom=cloud_bottom,
        future_span_a=future_a,
        future_span_b=future_b,
        price_position=price_position,
        tk_state=tk_state,
        kijun_slope=kijun_slope,
        chikou_state=chikou,
        future_cloud=future_cloud,
        kumo_breakout=breakout,
        cloud_thickness_pct=cloud_thickness,
        kijun_distance_pct=kijun_distance,
        volume_ratio=volume_ratio,
        avg_volume_20=avg_volume,
        avg_trade_value_20=avg_trade_value,
        candle_range_pct=candle_range_pct,
        flat_span_b_levels=flat_levels,
        volume_profile_levels=volume_levels,
        grade=grade,
        confidence=confidence,
        bullish_score=bullish,
        bearish_score=bearish,
        risk_score=risk_score,
        hard_blocks=tuple(dict.fromkeys(hard_blocks)),
        reasons=tuple(dict.fromkeys(reasons)),
        risks=tuple(dict.fromkeys(risks)),
        watch_price=watch_price,
        invalidation_price=max(0.01, invalidation),
        first_target_price=max(0.01, first_target),
        action=action,
    )


def apply_context(
    reading: IchimokuReading,
    *,
    weekly: IchimokuReading | None,
    market_label: str,
    market_is_weak: bool,
) -> IchimokuReading:
    blocks = list(reading.hard_blocks)
    risks = list(reading.risks)
    action = reading.action
    higher = "확인 불가"
    if weekly is not None:
        if weekly.grade in {"A+", "A"} and weekly.price_position == "구름 위":
            higher = "주봉도 상승 방향"
        elif weekly.price_position == "구름 아래" or weekly.grade == "D":
            higher = "주봉은 하락 방향"
            blocks.append("일봉보다 큰 주봉 흐름이 약합니다.")
            risks.append("일봉 반등이 주봉 하락에 막힐 수 있습니다.")
        else:
            higher = "주봉 방향이 뚜렷하지 않음"
            risks.append("상위 시간대인 주봉 확인이 부족합니다.")
    if market_is_weak:
        blocks.append("SPY와 QQQ 시장 흐름이 모두 약합니다.")
        risks.append("종목 차트가 좋아도 미국 시장 하락의 영향을 받을 수 있습니다.")
    elif "모두 상승" not in market_label:
        blocks.append("SPY와 QQQ가 함께 상승 방향인지 확인되지 않았습니다.")
        risks.append("미국 대형주 시장과 기술주 시장의 방향이 아직 맞지 않습니다.")
    if blocks and reading.grade in {"A+", "A", "B"}:
        action = "기다림 - 종목·주봉·시장 방향이 함께 좋아질 때 재확인"
    return replace(
        reading,
        hard_blocks=tuple(dict.fromkeys(blocks)),
        risks=tuple(dict.fromkeys(risks)),
        risk_score=reading.risk_score + max(0, len(blocks) - len(reading.hard_blocks)) * 2,
        action=action,
        higher_timeframe=higher,
        market_context=market_label,
    )


def _recent_cross(series: pd.Series, *, lookback: int) -> str:
    clean = series.dropna()
    if len(clean) < 2:
        return "없음"
    tail = clean.tail(lookback + 1)
    for previous, current in zip(tail.iloc[:-1], tail.iloc[1:]):
        if previous <= 0 < current:
            result = "상향 교차"
        elif previous >= 0 > current:
            result = "하향 교차"
        else:
            continue
    return locals().get("result", "없음")


def _cross_count(series: pd.Series) -> int:
    signs = np.sign(series.dropna().to_numpy(dtype=float))
    if len(signs) < 2:
        return 0
    signs = pd.Series(signs).replace(0, np.nan).ffill().fillna(0).to_numpy()
    return int(np.sum(signs[1:] != signs[:-1]))


def _flat_span_b_levels(series: pd.Series, current_price: float) -> tuple[float, ...]:
    values = series.dropna().tail(140).to_numpy(dtype=float)
    if len(values) < 5:
        return ()
    levels: list[tuple[int, float]] = []
    run_start = 0
    tolerance = max(current_price * 0.0005, 0.005)
    for index in range(1, len(values) + 1):
        if index < len(values) and abs(values[index] - values[index - 1]) <= tolerance:
            continue
        length = index - run_start
        if length >= 5:
            levels.append((length, float(np.median(values[run_start:index]))))
        run_start = index
    unique: list[float] = []
    for _, level in sorted(levels, key=lambda pair: (-pair[0], abs(pair[1] - current_price))):
        if all(abs(level - existing) > tolerance * 2 for existing in unique):
            unique.append(level)
        if len(unique) >= 4:
            break
    return tuple(sorted(unique))


def _volume_profile_levels(frame: pd.DataFrame, current_price: float) -> tuple[float, ...]:
    recent = frame.tail(140)
    if len(recent) < 30:
        return ()
    typical = ((recent["high"] + recent["low"] + recent["close"]) / 3.0).to_numpy(dtype=float)
    weights = recent["volume"].fillna(0.0).clip(lower=0.0).to_numpy(dtype=float)
    finite = np.isfinite(typical) & np.isfinite(weights) & (weights > 0)
    typical = typical[finite]
    weights = weights[finite]
    if len(typical) < 20 or float(weights.sum()) <= 0:
        return ()
    low, high = float(np.min(typical)), float(np.max(typical))
    if high <= low:
        return (low,) if low > 0 else ()
    histogram, edges = np.histogram(typical, bins=18, range=(low, high), weights=weights)
    centers = (edges[:-1] + edges[1:]) / 2.0
    ordered = np.argsort(histogram)[::-1]
    selected: list[float] = []
    minimum_gap = max(current_price * 0.01, (high - low) / 18.0)
    for index in ordered:
        if histogram[index] <= 0:
            continue
        level = float(centers[index])
        if all(abs(level - existing) >= minimum_gap for existing in selected):
            selected.append(level)
        if len(selected) >= 4:
            break
    return tuple(sorted(selected))


def _past_finite(series: pd.Series, periods: int) -> float | None:
    if len(series) <= periods:
        return None
    value = series.iloc[-1 - periods]
    return float(value) if _finite(value) else None


def _as_float(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _timestamp(value: Any) -> str:
    try:
        timestamp = pd.Timestamp(value)
    except Exception:
        return str(value)
    if timestamp.tzinfo is not None:
        return timestamp.strftime("%Y-%m-%d %H:%M %Z")
    if timestamp.hour or timestamp.minute or timestamp.second:
        return timestamp.strftime("%Y-%m-%d %H:%M")
    return timestamp.strftime("%Y-%m-%d")
