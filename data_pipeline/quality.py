from __future__ import annotations

"""Non-mutating quality checks for daily OHLCV data."""

from datetime import datetime, timezone
from typing import Iterable

import numpy as np
import pandas as pd

from .models import QualityIssue, QualityReport


REQUIRED_PRICE_COLUMNS = ("open", "high", "low", "close", "volume")


def inspect_daily_prices(
    frame: pd.DataFrame,
    *,
    market: str,
    symbol: str,
    timezone_name: str,
    listing_date: str | None = None,
    delisting_date: str | None = None,
    expected_sessions: Iterable[str | pd.Timestamp] | None = None,
    stale_after: str | pd.Timestamp | None = None,
    gap_threshold: float = 0.45,
) -> QualityReport:
    """Inspect without sorting, de-duplicating or correcting provider data."""

    issues: list[QualityIssue] = []
    now = datetime.now(timezone.utc).isoformat()
    missing_columns = [item for item in REQUIRED_PRICE_COLUMNS if item not in frame.columns]
    if missing_columns:
        issues.append(
            QualityIssue("error", "missing_columns", f"필수 열 누락: {', '.join(missing_columns)}", len(missing_columns))
        )
        return QualityReport(market, symbol, now, len(frame), issues, expected_sessions is not None)
    try:
        index = pd.DatetimeIndex(frame.index)
    except Exception:
        issues.append(QualityIssue("error", "invalid_datetime_index", "날짜 인덱스를 해석할 수 없습니다."))
        return QualityReport(market, symbol, now, len(frame), issues, expected_sessions is not None)

    if not index.is_monotonic_increasing:
        issues.append(QualityIssue("error", "not_sorted", "날짜가 오름차순이 아닙니다."))
    duplicates = int(index.duplicated(keep=False).sum())
    if duplicates:
        examples = tuple(str(item) for item in index[index.duplicated(keep=False)][:5])
        issues.append(QualityIssue("error", "duplicate_session", "중복 거래일이 있습니다.", duplicates, examples))
    weekend = index[index.weekday >= 5]
    if len(weekend):
        issues.append(
            QualityIssue("warning", "weekend_rows", "주말 날짜 행이 있습니다.", len(weekend), tuple(str(item) for item in weekend[:5]))
        )
    if index.tz is not None:
        actual = str(index.tz)
        if actual != timezone_name:
            issues.append(
                QualityIssue("warning", "timezone_mismatch", f"인덱스 timezone={actual}, manifest={timezone_name}")
            )

    numeric = frame.loc[:, REQUIRED_PRICE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    non_finite = int((~np.isfinite(numeric.to_numpy(dtype=float))).sum())
    if non_finite:
        issues.append(QualityIssue("error", "non_finite_values", "가격/거래량에 숫자가 아닌 값이 있습니다.", non_finite))
    prices = numeric.loc[:, ("open", "high", "low", "close")]
    non_positive = int((prices <= 0).sum().sum())
    if non_positive:
        issues.append(QualityIssue("error", "non_positive_price", "0 이하 가격이 있습니다.", non_positive))
    negative_volume = int((numeric["volume"] < 0).sum())
    if negative_volume:
        issues.append(QualityIssue("error", "negative_volume", "음수 거래량이 있습니다.", negative_volume))
    relation = (numeric["high"] < prices.max(axis=1)) | (numeric["low"] > prices.min(axis=1))
    if relation.any():
        bad = index[relation.fillna(False)]
        issues.append(
            QualityIssue("error", "invalid_ohlc_relation", "고가/저가와 시가/종가 관계가 잘못되었습니다.", int(relation.sum()), tuple(str(item) for item in bad[:5]))
        )

    closes = numeric["close"].replace(0, np.nan)
    gaps = closes.pct_change(fill_method=None).abs()
    suspicious = gaps[gaps >= gap_threshold]
    if len(suspicious):
        examples = tuple(f"{index[pos]}:{value:.1%}" for pos, value in zip(np.flatnonzero(gaps >= gap_threshold)[:5], suspicious.iloc[:5]))
        issues.append(QualityIssue("warning", "large_gap", "비정상 gap 또는 기업행사 의심 구간이 있습니다.", len(suspicious), examples))
    ratios = closes / closes.shift(1)
    split_like = ratios[(ratios.between(0.18, 0.55)) | (ratios.between(1.8, 5.5))]
    if len(split_like):
        issues.append(
            QualityIssue("warning", "suspected_split", "분할/병합 또는 데이터 오류 의심 구간이 있습니다.", len(split_like), tuple(str(item) for item in split_like.index[:5]))
        )

    if listing_date:
        before = index.date < pd.Timestamp(listing_date).date()
        if before.any():
            issues.append(QualityIssue("error", "before_listing", "상장일 이전 데이터가 있습니다.", int(before.sum())))
    if delisting_date:
        after = index.date > pd.Timestamp(delisting_date).date()
        if after.any():
            issues.append(QualityIssue("error", "after_delisting", "상장폐지일 이후 데이터가 있습니다.", int(after.sum())))
    if expected_sessions is not None:
        expected = pd.DatetimeIndex(pd.to_datetime(list(expected_sessions))).normalize()
        actual = pd.DatetimeIndex(index.tz_localize(None) if index.tz is not None else index).normalize()
        missing = expected.difference(actual)
        unexpected = actual.difference(expected)
        if len(missing):
            issues.append(
                QualityIssue("warning", "missing_exchange_session", "거래소 달력 기준 누락일이 있습니다.", len(missing), tuple(str(item.date()) for item in missing[:5]))
            )
        if len(unexpected):
            issues.append(
                QualityIssue("warning", "non_session_date", "거래소 달력에 없는 날짜가 있습니다.", len(unexpected), tuple(str(item.date()) for item in unexpected[:5]))
            )
    elif len(index) > 1:
        weekdays = pd.bdate_range(index.min().tz_localize(None).normalize(), index.max().tz_localize(None).normalize())
        missing_weekdays = weekdays.difference(index.tz_localize(None).normalize() if index.tz is not None else index.normalize())
        if len(missing_weekdays):
            issues.append(
                QualityIssue(
                    "info",
                    "possible_missing_or_holiday",
                    "거래소 휴장일 달력이 없어 평일 누락을 결측과 휴장으로 구분할 수 없습니다.",
                    len(missing_weekdays),
                    tuple(str(item.date()) for item in missing_weekdays[:5]),
                )
            )
    if stale_after is not None and len(index):
        cutoff = pd.Timestamp(stale_after)
        latest = pd.Timestamp(index.max())
        if latest.tzinfo is not None:
            latest = latest.tz_localize(None)
        if cutoff.tzinfo is not None:
            cutoff = cutoff.tz_localize(None)
        if latest.normalize() < cutoff.normalize():
            issues.append(QualityIssue("error", "stale_data", f"마지막 데이터 {latest.date()}가 요구일 {cutoff.date()}보다 오래되었습니다."))
    issues.extend(_corporate_action_issues(frame))
    return QualityReport(market, symbol, now, len(frame), issues, expected_sessions is not None)


def _corporate_action_issues(frame: pd.DataFrame) -> list[QualityIssue]:
    price_names = ("open", "high", "low", "close")
    raw = [f"raw_{name}" for name in price_names]
    adjusted = [f"adjusted_{name}" for name in price_names]
    raw_count = sum(name in frame for name in raw)
    adjusted_count = sum(name in frame for name in adjusted)
    if raw_count == adjusted_count == 0:
        return []
    if raw_count != len(raw) or adjusted_count != len(adjusted):
        return [
            QualityIssue(
                "error",
                "incomplete_adjustment_columns",
                "raw/adjusted OHLC는 네 가격 열을 모두 함께 제공해야 합니다.",
            )
        ]
    raw_frame = frame[raw].apply(pd.to_numeric, errors="coerce")
    adjusted_frame = frame[adjusted].apply(pd.to_numeric, errors="coerce")
    ratio = adjusted_frame.to_numpy(dtype=float) / raw_frame.to_numpy(dtype=float)
    row_spread = np.nanmax(ratio, axis=1) - np.nanmin(ratio, axis=1)
    inconsistent = np.isfinite(row_spread) & (row_spread > 1e-6)
    issues: list[QualityIssue] = []
    if inconsistent.any():
        issues.append(
            QualityIssue(
                "error",
                "inconsistent_ohlc_adjustment",
                "동일 세션에서 raw→adjusted 가격 배수가 OHLC마다 다릅니다.",
                int(inconsistent.sum()),
            )
        )
    if "adjustment_factor" not in frame:
        issues.append(
            QualityIssue(
                "warning",
                "adjustment_factor_missing",
                "adjusted 가격은 있으나 adjustment_factor 추적 열이 없습니다.",
            )
        )
        return issues
    factor = pd.to_numeric(frame["adjustment_factor"], errors="coerce").to_numpy(dtype=float)
    close_ratio = ratio[:, price_names.index("close")]
    mismatch = ~np.isclose(factor, close_ratio, rtol=1e-8, atol=1e-10, equal_nan=False)
    if mismatch.any():
        issues.append(
            QualityIssue(
                "error",
                "adjustment_factor_mismatch",
                "adjustment_factor는 adjusted_close/raw_close 가격 배수와 일치해야 합니다.",
                int(mismatch.sum()),
            )
        )
    return issues
