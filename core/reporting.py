from __future__ import annotations

"""Beginner-friendly Korean Markdown and HTML reports for US stock analysis."""

from dataclasses import dataclass
from datetime import datetime
from html import escape
from math import isfinite
import re
from typing import Iterable
from zoneinfo import ZoneInfo

from .ichimoku import IchimokuReading
from .kiwoom_rest import Quote, StockInfo
from .similarity import SimilarityResult


@dataclass(frozen=True)
class AnalyzedStock:
    stock: StockInfo
    daily: IchimokuReading
    similarity: SimilarityResult
    quote: Quote | None = None
    intraday: IchimokuReading | None = None
    liquid: bool = True
    liquidity_reason: str = ""


def render_html_report(
    markdown: str,
    *,
    title: str,
    footer: str = "Real2 · 키움 REST API 미국주식 · 일목 9·26·52",
) -> str:
    """Turn the analyzer's limited Markdown into a standalone, safe HTML report."""
    body = _markdown_blocks(markdown)
    safe_title = escape(title)
    safe_footer = escape(footer)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{safe_title}</title>
  <style>
    :root {{ --ink:#172033; --muted:#667085; --line:#e3e8ef; --paper:#fff;
      --bg:#f3f6fa; --navy:#102a43; --blue:#2563eb; --soft:#eef4ff; --warn:#fff7e6; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:var(--bg); font-family:-apple-system,
      BlinkMacSystemFont,"Segoe UI","Noto Sans KR","Malgun Gothic",sans-serif;
      font-size:15px; line-height:1.7; }}
    .top {{ height:7px; background:linear-gradient(90deg,var(--navy),var(--blue),#38bdf8); }}
    main {{ width:min(1120px,calc(100% - 32px)); margin:32px auto 56px; padding:42px 48px;
      background:var(--paper); border:1px solid var(--line); border-radius:18px;
      box-shadow:0 14px 40px rgba(16,42,67,.08); }}
    h1 {{ margin:0 0 26px; color:var(--navy); font-size:clamp(25px,4vw,38px); line-height:1.25; }}
    h2 {{ margin:38px 0 15px; padding-bottom:8px; border-bottom:2px solid var(--soft);
      color:var(--navy); font-size:21px; }}
    h3 {{ margin:28px 0 10px; color:var(--navy); font-size:17px; }}
    p {{ margin:10px 0; }}
    ul {{ margin:8px 0 16px; padding-left:22px; }}
    li {{ margin:5px 0; }}
    blockquote {{ margin:18px 0; padding:16px 20px; background:var(--soft);
      border-left:5px solid var(--blue); border-radius:8px; color:#243b53; }}
    blockquote p {{ margin:2px 0; }}
    .table-wrap {{ width:100%; margin:12px 0 22px; border:1px solid var(--line);
      border-radius:10px; }}
    table {{ width:100%; border-collapse:collapse; table-layout:fixed; font-size:13px; }}
    th {{ color:#fff; background:var(--navy); font-weight:650; text-align:left; }}
    th,td {{ padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:top;
      white-space:normal; overflow-wrap:anywhere; word-break:keep-all; }}
    tbody tr:nth-child(even) {{ background:#f8fafc; }}
    tbody tr:hover {{ background:#eef6ff; }}
    tbody tr:last-child td {{ border-bottom:0; }}
    th:nth-child(2),td:nth-child(2) {{ text-align:center; }}
    code {{ padding:2px 6px; border-radius:5px; background:#eef2f6; color:#334e68; }}
    strong {{ color:#0f3d75; }}
    .footer {{ margin-top:38px; color:var(--muted); font-size:12px; text-align:right; }}
    @media (max-width:760px) {{
      main {{ width:100%; margin:0; padding:26px 18px; border:0; border-radius:0; box-shadow:none; }}
      .top {{ height:5px; }} h2 {{ margin-top:30px; }}
      .table-wrap {{ border:0; }} table,tbody,tr,td {{ display:block; width:100%; }}
      thead {{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
        clip:rect(0,0,0,0); white-space:nowrap; border:0; }}
      tbody tr {{ margin:0 0 14px; padding:6px 12px; border:1px solid var(--line);
        border-radius:10px; background:#fff !important; box-shadow:0 3px 12px rgba(16,42,67,.05); }}
      tbody td {{ display:grid; grid-template-columns:minmax(92px,34%) 1fr; gap:10px;
        padding:8px 2px; text-align:left !important; border-bottom:1px solid var(--line); }}
      tbody td:last-child {{ border-bottom:0; }}
      tbody td::before {{ content:attr(data-label); color:var(--muted); font-weight:650; }}
    }}
    @media print {{ body {{ background:#fff; }} .top {{ display:none; }} main {{ width:100%; margin:0;
      padding:0; border:0; box-shadow:none; }} }}
  </style>
</head>
<body>
<div class="top"></div>
<main>
{body}
<div class="footer">{safe_footer}</div>
</main>
</body>
</html>
"""


def usd(value: float | None) -> str:
    if value is None or not isfinite(value):
        return "확인 불가"
    if abs(value) >= 100:
        return f"${value:,.2f}"
    if abs(value) >= 1:
        return f"${value:,.3f}"
    return f"${value:,.4f}"


def compact_money(value: float | None) -> str:
    if value is None or not isfinite(value):
        return "확인 불가"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return usd(value)


def render_individual_report(result: AnalyzedStock) -> str:
    stock = result.stock
    daily = result.daily
    quote = result.quote
    live_price = quote.price if quote is not None else daily.close
    price_label = "최근 조회 가격" if quote is not None else "분석 일봉 종가"
    price_context = (
        f"조회 시각 {quote.timestamp.astimezone(ZoneInfo('America/New_York')):%Y-%m-%d %H:%M:%S %Z}; 실제 체결 시각과 다를 수 있습니다."
        if quote is not None
        else f"{daily.data_timestamp} 완료 일봉 기준이며 현재가를 조회한 값이 아닙니다."
    )
    reasons = list(daily.reasons[:3]) or ["아직 뚜렷한 상승 확인 신호가 없습니다."]
    blocks = list(daily.hard_blocks[:3])
    risks = list(daily.risks[:3])
    similarity = _similarity_text(result.similarity)
    intraday = _intraday_text(result.intraday)
    support_text = (
        "이 가격 부근을 지키는지 확인하세요. 현재가에서 무조건 사라는 뜻이 아닙니다."
        if live_price >= daily.watch_price
        else "현재가가 이 가격을 종가로 다시 넘어야 상승 확인이 좋아집니다."
    )
    invalidation_text = (
        "일봉 종가가 이 아래로 내려가면 지금의 상승 시나리오는 폐기합니다."
    )
    target_text = (
        "도달 보장이 아닌 첫 저항 후보입니다."
        if daily.first_target_price is not None
        else "확인할 수 있는 상단 저항이 없어 목표 가격을 제시하지 않습니다."
    )
    flat_text = ", ".join(usd(value) for value in daily.flat_span_b_levels) or "뚜렷한 수평 구간 없음"
    volume_level_text = ", ".join(usd(value) for value in daily.volume_profile_levels) or "뚜렷한 집중 구간 없음"
    lines = [
        f"# {stock.display_name} ({stock.symbol}) 미국주식 분석",
        "",
        f"> **지금 할 일: {daily.action}**",
        f"> 일목 등급 {daily.grade} · 신뢰도 {daily.confidence} · {price_label} {usd(live_price)}",
        "",
        "## 왜 이렇게 봤나요?",
        "",
    ]
    lines.extend(f"- {reason}" for reason in reasons)
    if blocks:
        lines.append(f"- **아직 기다리는 이유:** {' / '.join(blocks)}")
    if not result.liquid:
        lines.append(f"- **유동성 경고:** {result.liquidity_reason}")
    lines.extend(
        [
            "",
            "## 가격은 이것만 보세요",
            "",
            "| 항목 | 가격 | 쉬운 뜻 |",
            "|---|---:|---|",
            f"| {price_label} | {usd(live_price)} | {price_context} |",
            f"| 분석 기준 종가 | {usd(daily.close)} | {daily.data_timestamp} 완료 일봉; 등급·손익비·가격 기준의 계산값입니다. |",
            f"| 확인할 가격 | {usd(daily.watch_price)} | {support_text} |",
            f"| 시나리오 무효 가격 | {usd(daily.invalidation_price)} | {invalidation_text} |",
            f"| 첫 저항·목표 후보 | {usd(daily.first_target_price)} | {target_text} |",
            "",
            "## 거래량·큰 흐름",
            "",
            f"- 거래량: {_volume_text(daily.volume_ratio)}",
            f"- 변동성: ATR(14) {daily.atr14_pct:.2f}% · 분석 완료 캔들 {daily.candle_range_atr:.2f} ATR",
            f"- 추세 강도: ADX(14) {daily.adx14:.1f} · +DI {daily.plus_di14:.1f} / -DI {daily.minus_di14:.1f}",
            f"- 첫 저항까지 손익비: {daily.reward_risk_ratio:.2f}:1 (실제 저항이 없으면 0으로 보수적 표시)",
            "- 거래량·손익비는 분석 완료 일봉 기준이며 이후 현재가 변동을 반영하지 않습니다.",
            f"- 최근 20일 평균 거래대금: {compact_money(daily.avg_trade_value_20)}",
            f"- 주봉: {daily.higher_timeframe}",
            f"- 미국 시장: {daily.market_context}",
            f"- 선행 스팬 B 수평 가격대: {flat_text}",
            f"- 과거 거래량 집중 가격대: {volume_level_text}",
            "",
            "## 현재 지표와 최대한 같은 과거 패턴",
            "",
            f"- {similarity}",
            "- 가격의 구름 위치, 미래 구름, 전환선·기준선 관계, 기준선 방향, 후행 스팬 상태가 모두 같은 과거만 사용합니다.",
            "- 그 안에서 RSI·이동평균·모멘텀·거래량·캔들 폭이 어느 한 항목도 크게 다르지 않은 순서로 최대 30건을 선택합니다.",
            "- 구조 지지선 이탈은 당시 가격 아래의 전환선·기준선·구름 경계 중 가장 가까운 값을 기준으로 이후 10거래일 저가를 확인한 연구용 통계입니다.",
            "- 기준: 신호 다음 거래일 시가에 들어갔다고 가정하고 10거래일째 종가가 더 높았는지 계산합니다.",
            "- 이 통계는 참고 자료이며 미래 상승 확률이나 수익을 보장하지 않습니다.",
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
        lines.append("- 차트에 특별한 추가 경고는 없지만 실적 발표와 미국 경제 일정을 별도로 확인해야 합니다.")
    else:
        lines.append("- 실적 발표, FOMC, 고용지표 같은 일정은 이 차트 데이터만으로 확인할 수 없습니다.")
    lines.extend(
        [
            "",
            "## 분석 정보",
            "",
            f"- 종목/거래소: {stock.symbol} / {stock.exchange}",
            f"- 분석 일봉: {daily.data_timestamp}",
            f"- 데이터 범위: {daily.source_range}",
            "- 일목 파라미터: 9, 26, 52 (26기간 이동)",
            "- 가격 통화: USD",
            "- 기준 시간대: America/New_York (미국 동부시간)",
            "- 데이터 출처: 키움 REST API 미국주식",
            "",
            "> 이 보고서는 조건부 차트 시나리오이며 투자 권유가 아닙니다.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_scan_report(
    results: Iterable[AnalyzedStock],
    *,
    started_at: datetime,
    finished_at: datetime,
    universe_stats: dict[str, int],
    failures: list[str],
) -> str:
    values = list(results)
    interests = [item for item in values if _is_interest(item)]
    waits = [item for item in values if item.daily.grade in {"A+", "A", "B"} and item not in interests]
    avoids = [item for item in values if item not in interests and item not in waits]
    interests.sort(key=_sort_key)
    waits.sort(key=_sort_key)
    avoids.sort(key=_sort_key)
    elapsed = finished_at - started_at
    lines = [
        "# 미국주식 전체 분석",
        "",
        f"> **결론: 관심 후보 {len(interests)}개 · 기다릴 종목 {len(waits)}개 · 피할 종목 {len(avoids)}개**",
        "> ETF·ETN·워런트·우선주·스팩과 유동성 부족 종목은 관심 후보에서 제외했습니다.",
        "",
        "## 1. 먼저 볼 관심 후보",
        "",
    ]
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
            f"- 순위 원본 중복 제거: {universe_stats.get('ranked_unique', 0):,}개",
            f"- 저가·비일반주 제외: {universe_stats.get('excluded_non_common_or_low_price', 0):,}개",
            f"- 분석 대상으로 선별: {universe_stats.get('selected', len(values)):,}개",
            f"- 실제 분석 완료: {len(values):,}개",
            f"- 데이터 오류: {len(failures):,}개",
            f"- 소요 시간: {str(elapsed).split('.')[0]}",
            f"- 시작/종료: {started_at.astimezone(ZoneInfo('America/New_York')):%Y-%m-%d %H:%M:%S %Z} / {finished_at.astimezone(ZoneInfo('America/New_York')):%Y-%m-%d %H:%M:%S %Z}",
            "- 가격 통화/기준 시간대: USD / America/New_York (미국 동부시간)",
            "- 선별 방식: 시가총액 상위 + 거래대금 상위의 합집합을 업종별로 분산",
            "- 일목 파라미터: 9, 26, 52 / 일봉 주 분석 + 주봉 확인",
            "- 과거 지표 일치 표본: 일목 핵심 상태 모두 일치 후 RSI·SMA·모멘텀·거래량·캔들 폭도 항목별로 최대한 같은 과거 최대 30건",
            "- 데이터 출처: 키움 REST API 미국주식",
            "- 화면에 생략된 종목까지 포함한 전체 값은 같은 폴더의 `all_results.csv`에 저장됩니다.",
        ]
    )
    if failures:
        lines.extend(["", "### 끝까지 재시도했지만 확인하지 못한 종목", ""])
        lines.extend(f"- {message}" for message in failures)
    lines.extend(
        [
            "",
            "> A+도 수익 보장이 아닙니다. 관심 후보는 거래량·주봉·시장 방향까지 맞은 차트 후보일 뿐입니다.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def one_line(result: AnalyzedStock) -> str:
    similarity = result.similarity
    rate = (
        f"{similarity.up_count}/{similarity.sample_count} 상승"
        if similarity.sample_count
        else "지표 일치 표본 부족"
    )
    return (
        f"{result.stock.display_name} ({result.stock.symbol}) | {result.daily.grade} | "
        f"{result.daily.action} | {rate}"
    )


def _is_interest(item: AnalyzedStock) -> bool:
    return (
        item.liquid
        and item.daily.grade in {"A+", "A"}
        and not item.daily.hard_blocks
        and item.daily.higher_timeframe == "주봉도 상승 방향"
        and "모두 상승" in item.daily.market_context
    )


def _sort_key(item: AnalyzedStock) -> tuple[int, int, float, float, float, float, float]:
    grade_order = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    rate = item.similarity.up_rate if item.similarity.up_rate is not None else -1.0
    expected = (
        item.similarity.expected_return_pct
        if item.similarity.expected_return_pct is not None and item.similarity.sample_count >= 15
        else -999.0
    )
    invalidation = (
        item.similarity.invalidation_rate
        if item.similarity.invalidation_rate is not None
        else 100.0
    )
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
        "| 종목 | 등급 | 지금 할 일 | 손익비/ADX | 확인/무효/목표 | 과거 지표 일치 10일 | 평균 거래대금 | 분석 일봉 종가 · 데이터 범위 |",
        "|---|:---:|---|---|---|---|---:|---|",
    ]
    for item in items[:limit]:
        daily = item.daily
        similarity = item.similarity
        past = (
            f"{similarity.up_count}/{similarity.sample_count} 상승 ({similarity.up_rate:.1f}%)"
            if similarity.up_rate is not None
            else "표본 부족"
        )
        if similarity.sample_count:
            past += f" · {similarity.match_level}"
            if similarity.invalidation_rate is not None:
                past += f" · 구조선 이탈 {similarity.invalidation_rate:.1f}%"
        action = daily.action.replace("|", "/")
        if not item.liquid:
            action = f"유동성 부족: {item.liquidity_reason}"
        levels = f"{usd(daily.watch_price)} / {usd(daily.invalidation_price)} / {usd(daily.first_target_price)}"
        quality = f"{daily.reward_risk_ratio:.2f}:1 / {daily.adx14:.1f}"
        cells = (
            f"{item.stock.display_name} ({item.stock.symbol})", daily.grade, action,
            quality, levels, past, compact_money(daily.avg_trade_value_20),
            f"{usd(daily.close)} · {daily.source_range}",
        )
        lines.append("| " + " | ".join(_table_cell(cell) for cell in cells) + " |")
    if len(items) > limit:
        lines.append(f"\n표가 길어 나머지 {len(items) - limit}개는 생략했습니다.")
    return lines


def _similarity_text(result: SimilarityResult) -> str:
    if result.up_rate is None:
        return f"과거 표본 내 결과: {result.sample_count}건 · {result.status}"
    recent = (
        f" 최근 표본은 {result.recent_up_count}/{result.recent_sample_count}건 상승"
        if result.recent_sample_count
        else ""
    )
    average_up = (
        f"평균 상승 {result.average_up_return_pct:+.2f}%"
        if result.average_up_return_pct is not None
        else "상승 사례 없음"
    )
    average_down = (
        f"평균 하락 {result.average_down_return_pct:+.2f}%"
        if result.average_down_return_pct is not None
        else "하락 사례 없음"
    )
    invalidation = (
        f"구조 지지선 선행 이탈 {result.invalidation_count}/{result.sample_count}건"
        f"({result.invalidation_rate:.1f}%)"
        if result.invalidation_rate is not None
        else "구조 지지선 이탈 확인 불가"
    )
    expected = (
        f"과거 표본 평균 수익률 {result.expected_return_pct:+.2f}%"
        if result.expected_return_pct is not None
        else "과거 표본 평균 수익률 확인 불가"
    )
    downside = (
        f", 하위 25% 수익률 {result.downside_p25_pct:+.2f}%"
        if result.downside_p25_pct is not None
        else ""
    )
    payoff = (
        f", 상승/하락 손익비 {result.payoff_ratio:.2f}"
        if result.payoff_ratio is not None
        else ""
    )
    median = (
        f"{result.median_return_pct:+.2f}%"
        if result.median_return_pct is not None
        else "확인 불가"
    )
    return (
        f"표본 조건: {result.match_level} · 조건 통과 과거 {result.candidate_count}건 중 "
        f"각 보조지표까지 최대한 같은 {result.sample_count}건을 사용했습니다. "
        f"과거 표본 내 결과: {result.sample_count}건 중 {result.up_count}건 상승({result.up_rate:.1f}%), "
        f"중간 수익률 {median}, {average_up}, {average_down}, "
        f"{expected}{downside}{payoff}, {invalidation}.{recent} · {result.status}"
    )


def _intraday_text(reading: IchimokuReading | None) -> str:
    if reading is None:
        return "60분봉 데이터를 충분히 확인하지 못했습니다. 일봉 판단만 사용하세요."
    return (
        f"60분봉은 {reading.price_position}, {reading.tk_state}입니다. "
        f"등급은 {reading.grade}이며 일봉과 반대면 진입 타이밍을 늦추는 편이 안전합니다."
    )


def _volume_text(ratio: float | None) -> str:
    if ratio is None:
        return "평균과 비교할 데이터가 부족합니다."
    if ratio >= 1.2:
        return f"최근 20일 평균의 {ratio:.2f}배로 충분한 편입니다."
    if ratio >= 0.8:
        return f"최근 20일 평균의 {ratio:.2f}배로 보통입니다."
    return f"최근 20일 평균의 {ratio:.2f}배로 부족한 편입니다."


def _inline_markdown(text: str) -> str:
    safe = escape(text)
    safe = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)
    safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
    return safe


def _table_cell(text: object) -> str:
    """Keep provider text inside a single Markdown table cell."""
    return " ".join(str(text).replace("|", "｜").split())


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _markdown_blocks(markdown: str) -> str:
    lines = markdown.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            level = len(heading.group(1))
            output.append(f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>")
            index += 1
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote_lines.append(lines[index].strip()[1:].strip())
                index += 1
            content = "".join(f"<p>{_inline_markdown(item)}</p>" for item in quote_lines)
            output.append(f"<blockquote>{content}</blockquote>")
            continue

        if (
            stripped.startswith("|")
            and index + 1 < len(lines)
            and _is_table_separator(lines[index + 1].strip())
        ):
            headers = [cell.strip() for cell in stripped.strip("|").split("|")]
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append([cell.strip() for cell in lines[index].strip().strip("|").split("|")])
                index += 1
            head = "".join(f"<th>{_inline_markdown(cell)}</th>" for cell in headers)
            row_html = "".join(
                "<tr>"
                + "".join(
                    f'<td data-label="{escape(headers[position] if position < len(headers) else "")}">'
                    f"{_inline_markdown(cell)}</td>"
                    for position, cell in enumerate(row)
                )
                + "</tr>"
                for row in rows
            )
            output.append(
                f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
                f"<tbody>{row_html}</tbody></table></div>"
            )
            continue

        if stripped.startswith("- "):
            items: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("- "):
                items.append(lines[index].strip()[2:].strip())
                index += 1
            output.append("<ul>" + "".join(f"<li>{_inline_markdown(item)}</li>" for item in items) + "</ul>")
            continue

        paragraph: list[str] = [stripped]
        index += 1
        while index < len(lines) and lines[index].strip():
            candidate = lines[index].strip()
            if candidate.startswith(("#", ">", "- ", "|")):
                break
            paragraph.append(candidate)
            index += 1
        output.append(f"<p>{_inline_markdown(' '.join(paragraph))}</p>")
    return "\n".join(output)
