from __future__ import annotations

"""Korean-market Markdown reporting using KRW prices."""

from datetime import datetime
from math import isfinite
from typing import Iterable
from zoneinfo import ZoneInfo

from .market_intelligence import MarketIntelligence
from .reporting import (
    AnalyzedStock,
    _intelligence_lines,
    _market_intelligence_summary_lines,
    _table_cell,
    final_action_bucket,
    similarity_sample_note,
    watch_price_display,
)
from .similarity import SimilarityResult


def krw(value: float | None) -> str:
    if value is None or not isfinite(value):
        return "확인 불가"
    return f"₩{value:,.0f}"


def compact_krw(value: float | None) -> str:
    if value is None or not isfinite(value):
        return "확인 불가"
    if value >= 1_000_000_000_000:
        return f"₩{value / 1_000_000_000_000:.2f}조"
    if value >= 100_000_000:
        return f"₩{value / 100_000_000:.1f}억"
    if value >= 10_000:
        return f"₩{value / 10_000:.1f}만"
    return krw(value)


def render_domestic_individual_report(result: AnalyzedStock) -> str:
    stock = result.stock
    daily = result.daily
    quote = result.quote
    live_price = quote.price if quote is not None else daily.close
    price_label = "최근 조회 가격" if quote is not None else "분석 일봉 종가"
    price_context = (
        f"조회 시각 {quote.timestamp.astimezone(ZoneInfo('Asia/Seoul')):%Y-%m-%d %H:%M:%S %Z}; 실제 체결 시각과 다를 수 있습니다."
        if quote is not None
        else f"{daily.data_timestamp} 완료 일봉 기준이며 현재가를 조회한 값이 아닙니다."
    )
    watch_display = watch_price_display(
        current_price=live_price,
        watch_price=daily.watch_price,
        invalidation_price=daily.invalidation_price,
    )
    target_text = (
        "도달 보장이 아닌 첫 저항 후보입니다."
        if daily.first_target_price is not None
        else "확인할 수 있는 상단 저항이 없어 목표 가격을 제시하지 않습니다."
    )
    reasons = list(daily.reasons[:3]) or ["아직 뚜렷한 상승 확인 신호가 없습니다."]
    blocks = list(daily.hard_blocks[:3])
    risks = list(daily.risks[:3])
    similarity = _similarity_text(result.similarity)
    intraday = _intraday_text(result.intraday)
    flat_text = ", ".join(krw(value) for value in daily.flat_span_b_levels) or "뚜렷한 수평 구간 없음"
    volume_level_text = (
        ", ".join(krw(value) for value in daily.volume_profile_levels)
        or "뚜렷한 집중 구간 없음"
    )
    lines = [
        f"# {stock.display_name} ({stock.symbol}) 국내주식 분석",
        "",
        f"> **지금 할 일: {result.final_action}**",
        f"> 일목 등급 {daily.grade} · 신뢰도 {daily.confidence} · 보조 종합점수 {result.context_adjusted_score}/100 · {price_label} {krw(live_price)}",
        "",
        "## 왜 이렇게 봤나요?",
        "",
    ]
    lines.extend(f"- {reason}" for reason in reasons)
    if blocks:
        lines.append(f"- **아직 기다리는 이유:** {' / '.join(blocks)}")
    if not result.liquid:
        lines.append(f"- **유동성 경고:** {result.liquidity_reason}")
    lines.extend(_intelligence_lines(result.intelligence))
    lines.extend(
        [
            "",
            "## 가격은 이것만 보세요",
            "",
            "| 항목 | 가격 | 쉬운 뜻 |",
            "|---|---:|---|",
            f"| {price_label} | {krw(live_price)} | {price_context} |",
            f"| 분석 기준 종가 | {krw(daily.close)} | {daily.data_timestamp} 완료 일봉; 등급과 가격 기준의 계산값입니다. |",
            f"| {watch_display.label} | {krw(daily.watch_price)} | {watch_display.explanation} "
            f"(상태: {watch_display.state}) |",
            f"| 하락 경계가 | {krw(daily.invalidation_price)} | 일봉 종가가 이 아래면 현재 상승 시나리오를 폐기합니다. |",
            f"| 첫 저항가 | {krw(daily.first_target_price)} | {target_text} |",
            "",
            "## 거래량·큰 흐름",
            "",
            f"- 거래량: {_volume_text(daily.volume_ratio)}",
            f"- 변동성: ATR(14) {daily.atr14_pct:.2f}% · 분석 완료 캔들 {daily.candle_range_atr:.2f} ATR",
            f"- 추세 강도: ADX(14) {daily.adx14:.1f} · +DI {daily.plus_di14:.1f} / -DI {daily.minus_di14:.1f}",
            "- 거래량·기술지표는 분석 완료 일봉 기준이며 이후 현재가 변동을 반영하지 않습니다.",
            f"- 최근 20일 평균 거래대금: {compact_krw(daily.avg_trade_value_20)}",
            f"- 주봉: {daily.higher_timeframe}",
            f"- 국내 시장: {daily.market_context}",
            f"- 선행 스팬 B 수평 가격대: {flat_text}",
            f"- 과거 거래량 집중 가격대: {volume_level_text}",
            "",
            "## 현재 지표와 최대한 같은 과거 패턴",
            "",
            f"- {similarity}",
            "- 일목 핵심 구조가 같은 과거에서 RSI·이동평균·모멘텀·ATR·ADX·거래량까지 가까운 사례를 찾습니다.",
            "- 10거래일 결과가 서로 겹치지 않도록 과거 표본 사이에 10거래일 간격을 둡니다.",
            "- 신호 다음 거래일 시가 대비 10거래일째 종가 수익률을 연구용으로 계산합니다.",
            "- 이 통계는 미래 상승 확률이나 수익을 보장하지 않습니다.",
            "",
            "## 짧은 시간 흐름",
            "",
            f"- {intraday}",
            "",
            "## 위험 요인",
            "",
        ]
    )
    lines.extend(f"- {risk}" for risk in risks)
    if not risks:
        lines.append("- 차트상 추가 경고는 적지만 실적 발표와 국내외 주요 경제 일정을 별도로 확인해야 합니다.")
    lines.extend(
        [
            "",
            "## 분석 정보",
            "",
            f"- 종목/시장: {stock.symbol} / {stock.exchange}",
            f"- 분석 완료 일봉: {daily.data_timestamp}",
            f"- 데이터 범위: {daily.source_range}",
            "- 일목 파라미터: 9, 26, 52 (26기간 이동)",
            "- 가격 통화: KRW",
            "- 기준 시간대: Asia/Seoul (한국시간)",
            "- 데이터 출처: 키움 REST API 국내주식",
            "",
            "> 이 보고서는 조건부 차트 시나리오이며 투자 권유가 아닙니다.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_domestic_scan_report(
    results: Iterable[AnalyzedStock],
    *,
    started_at: datetime,
    finished_at: datetime,
    universe_stats: dict[str, int],
    failures: list[str],
    intelligence: MarketIntelligence | None = None,
) -> str:
    values = list(results)
    interests = [item for item in values if final_action_bucket(item.final_action) == "interest"]
    waits = [item for item in values if final_action_bucket(item.final_action) == "wait"]
    avoids = [item for item in values if final_action_bucket(item.final_action) == "avoid"]
    interests.sort(key=_sort_key)
    waits.sort(key=_sort_key)
    avoids.sort(key=_sort_key)
    elapsed = finished_at - started_at
    lines = [
        "# 국내주식 전체 분석",
        "",
        f"> **결론: 관심 후보 {len(interests)}개 · 기다릴 종목 {len(waits)}개 · 피할 종목 {len(avoids)}개**",
        "> KOSPI·KOSDAQ 거래대금 상위에서 우선주·스팩·관리/경고 종목과 유동성 부족 종목을 제외했습니다.",
        "> 등급과 표시 순서는 분석 규칙에 따른 분류이며, 전체 선별 전략의 수익성이 검증되었다는 뜻은 아닙니다.",
        "",
        "> **가격 기준 읽는 법:** 눌림 지지 = 조정 시 지지 확인 · 회복 확인 = 위로 넘어야 할 가격 · 시나리오 밖 = 하락 경계보다 낮아 대기 기준으로 쓰지 않음 · 첫 저항 = 위에서 막힐 수 있는 가격",
        "",
    ]
    lines.extend(_market_intelligence_summary_lines(intelligence))
    lines.extend(["", "## 1. 먼저 볼 관심 후보", ""])
    lines.extend(_scan_table(interests, empty="현재 조건을 모두 통과한 관심 후보가 없습니다."))
    lines.extend(["", "## 2. 차트는 일부 좋지만 기다릴 종목", ""])
    lines.extend(_scan_table(waits, empty="없음", limit=60))
    lines.extend(["", "## 3. 지금은 피할 종목", ""])
    lines.extend(_scan_table(avoids, empty="없음", limit=40))
    lines.extend(
        [
            "",
            "## 선별·완료 정보",
            "",
            f"- 거래대금 순위 중복 제거: {universe_stats.get('ranked_unique', 0):,}개",
            f"- 제외된 비일반주·저가 후보: {universe_stats.get('excluded_non_common_or_low_price', 0):,}개",
            f"- 분석 대상으로 선별: {universe_stats.get('selected', len(values)):,}개",
            f"- KOSPI / KOSDAQ: {universe_stats.get('kospi_selected', 0):,} / {universe_stats.get('kosdaq_selected', 0):,}",
            f"- 실제 분석 완료: {len(values):,}개",
            f"- 신규상장·데이터 기간 부족 자동 제외: {universe_stats.get('insufficient_history', 0):,}개",
            f"- 데이터 오류: {len(failures):,}개",
            f"- 소요 시간: {str(elapsed).split('.')[0]}",
            f"- 시작/종료: {started_at.astimezone(ZoneInfo('Asia/Seoul')):%Y-%m-%d %H:%M:%S %Z} / {finished_at.astimezone(ZoneInfo('Asia/Seoul')):%Y-%m-%d %H:%M:%S %Z}",
            "- 가격 통화/기준 시간대: KRW / Asia/Seoul (한국시간)",
            "- 선별 방식: KOSPI·KOSDAQ 거래대금 상위의 합집합을 시장·업종별로 분산",
            "- 일목 파라미터: 9, 26, 52 / 일봉 주 분석 + 주봉 확인",
            "- 보조지표: ATR(14), ADX/+DI/-DI, 거래량, 과거 유사패턴",
            "- 데이터 출처: 키움 REST API 국내주식",
            "- 전체 수치는 같은 폴더의 `all_results.csv`에 저장됩니다.",
        ]
    )
    if failures:
        lines.extend(["", "### 끝까지 재시도했지만 확인하지 못한 종목", ""])
        lines.extend(f"- {message}" for message in failures)
    lines.extend(
        [
            "",
            "> 관심 후보도 수익 보장이 아닙니다. 시장·주봉·유동성과 상승 구조를 함께 통과한 차트 후보입니다.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _is_interest(item: AnalyzedStock) -> bool:
    return item.is_interest


def _sort_key(item: AnalyzedStock) -> tuple[int, int, float, float, float, float, float]:
    grade_order = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    similarity = item.similarity
    expected = (
        similarity.expected_return_pct
        if similarity.expected_return_pct is not None and similarity.sample_count >= 15
        else -999.0
    )
    invalidation = similarity.invalidation_rate if similarity.invalidation_rate is not None else 100.0
    rate = similarity.up_rate if similarity.up_rate is not None else -1.0
    value = item.daily.avg_trade_value_20 or 0.0
    return (
        grade_order.get(item.daily.grade, 9),
        len(item.daily.hard_blocks),
        -expected,
        invalidation,
        -item.daily.reward_risk_ratio,
        -rate,
        -value,
    )


def _scan_table(items: list[AnalyzedStock], *, empty: str, limit: int = 80) -> list[str]:
    if not items:
        return [empty]
    lines = [
        "| 종목 | 등급 | 판단 | 가격 기준 | 과거 유사 패턴 | 평균 거래대금 | 기준 일봉 · 데이터 |",
        "|---|:---:|---|---|---|---:|---|",
    ]
    for item in items[:limit]:
        daily = item.daily
        similarity = item.similarity
        sample_note = similarity_sample_note(similarity.sample_count)
        if similarity.sample_count < 10:
            past = f"{similarity.sample_count}건 · {sample_note}"
        else:
            past = (
                f"{similarity.sample_count}건 중 {similarity.up_count}건 상승"
                if similarity.up_rate is not None
                else "표본 부족"
            )
            if similarity.sample_count and similarity.expected_return_pct is not None:
                past += f" · 평균 {similarity.expected_return_pct:+.2f}%"
            past += f" · {sample_note}"
        action = f"보조 {item.context_adjusted_score}/100 · {item.final_action}".replace("|", "/")
        if not item.liquid:
            action = f"유동성 부족: {item.liquidity_reason}"
        watch = watch_price_display(
            current_price=daily.close,
            watch_price=daily.watch_price,
            invalidation_price=daily.invalidation_price,
        )
        watch_level = (
            f"관찰 기준 N/A (계산값 {krw(daily.watch_price)}은 시나리오 밖)"
            if watch.state == "INVALID_WATCH_ZONE"
            else f"{watch.label.removesuffix('가')} {krw(daily.watch_price)}"
        )
        levels = (
            f"{watch_level} · "
            f"하락 경계 {krw(daily.invalidation_price)} · "
            f"첫 저항 {krw(daily.first_target_price)}"
        )
        cells = (
            f"{item.stock.display_name} ({item.stock.symbol}) · {item.stock.exchange}",
            daily.grade, action, levels, past,
            compact_krw(daily.avg_trade_value_20),
            f"종가 {krw(daily.close)} · {daily.source_range}",
        )
        lines.append("| " + " | ".join(_table_cell(cell) for cell in cells) + " |")
    if len(items) > limit:
        lines.append(f"\n표가 길어 나머지 {len(items) - limit}개는 생략했습니다.")
    return lines


def _similarity_text(result: SimilarityResult) -> str:
    sample_note = similarity_sample_note(result.sample_count)
    if result.sample_count < 10:
        return (
            f"과거 표본 내 결과: 독립 표본 {result.sample_count}건 · {sample_note}. "
            "수익률과 상승 비율은 표본이 너무 적어 표시하지 않습니다."
        )
    if result.up_rate is None:
        return f"과거 표본 내 결과: {result.sample_count}건 · {sample_note}"
    expected = (
        f"과거 표본 평균 수익률 {result.expected_return_pct:+.2f}%"
        if result.expected_return_pct is not None
        else "과거 표본 평균 수익률 확인 불가"
    )
    downside = (
        f" · 하위 25% {result.downside_p25_pct:+.2f}%"
        if result.downside_p25_pct is not None
        else ""
    )
    invalidation = (
        f" · 구조선 이탈 {result.invalidation_rate:.1f}%"
        if result.invalidation_rate is not None
        else ""
    )
    median = (
        f"{result.median_return_pct:+.2f}%"
        if result.median_return_pct is not None
        else "확인 불가"
    )
    return (
        f"과거 표본 내 결과: 독립 표본 {result.sample_count}건 중 {result.up_count}건 상승({result.up_rate:.1f}%), "
        f"중간 수익률 {median}, {expected}{downside}{invalidation} · {sample_note}"
    )


def _intraday_text(reading) -> str:
    if reading is None:
        return "60분봉 데이터를 충분히 확인하지 못했습니다. 일봉 판단만 사용하세요."
    return (
        f"60분봉은 {reading.price_position}, {reading.tk_state}, ADX {reading.adx14:.1f}입니다. "
        "일봉과 반대면 진입 타이밍을 늦춰 확인합니다."
    )


def _volume_text(ratio: float | None) -> str:
    if ratio is None:
        return "평균과 비교할 데이터가 부족합니다."
    if ratio >= 1.2:
        return f"최근 20일 평균의 {ratio:.2f}배로 강한 편입니다."
    if ratio >= 0.8:
        return f"최근 20일 평균의 {ratio:.2f}배로 보통입니다."
    return f"최근 20일 평균의 {ratio:.2f}배로 부족한 편입니다."
