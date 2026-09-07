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
    :root {{ --ink:#172033; --muted:#667085; --line:#dfe5ec; --paper:#fff;
      --bg:#eef2f7; --navy:#132a46; --blue:#2563eb; --soft:#f2f6ff;
      --green:#067647; --green-soft:#ecfdf3; --amber:#b54708; --amber-soft:#fff7ed;
      --red:#b42318; --red-soft:#fef3f2; --slate:#475467; --slate-soft:#f2f4f7; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:var(--bg); font-family:-apple-system,
      BlinkMacSystemFont,"Segoe UI","Noto Sans KR","Malgun Gothic",sans-serif;
      font-size:15px; line-height:1.68; -webkit-font-smoothing:antialiased; }}
    .top {{ height:6px; background:linear-gradient(90deg,var(--navy),var(--blue),#38bdf8); }}
    main {{ width:min(1680px,calc(100% - 48px)); margin:30px auto 56px; padding:38px 44px 42px;
      background:var(--paper); border:1px solid var(--line); border-radius:20px;
      box-shadow:0 18px 48px rgba(16,42,67,.09); }}
    .report-kicker {{ display:inline-flex; align-items:center; gap:8px; margin-bottom:14px;
      color:#526174; font-size:11px; font-weight:750; letter-spacing:.14em; text-transform:uppercase; }}
    .report-kicker::before {{ content:""; width:8px; height:8px; border-radius:50%; background:var(--blue);
      box-shadow:0 0 0 4px #eaf1ff; }}
    h1 {{ margin:0 0 24px; color:var(--navy); font-size:clamp(28px,3vw,40px); line-height:1.2;
      letter-spacing:-.03em; }}
    h2 {{ margin:42px 0 16px; padding:0 0 10px; border-bottom:1px solid #e6ebf1;
      color:var(--navy); font-size:22px; line-height:1.35; letter-spacing:-.02em; }}
    h3 {{ margin:30px 0 12px; color:var(--navy); font-size:18px; }}
    p {{ margin:10px 0; }}
    ul {{ margin:8px 0 16px; padding-left:22px; }}
    li {{ margin:5px 0; }}
    li::marker {{ color:#7b8da3; }}
    blockquote {{ margin:18px 0; padding:17px 20px; background:linear-gradient(135deg,#f5f8ff,#eef4ff);
      border:1px solid #dce7ff; border-left:4px solid var(--blue); border-radius:12px; color:#243b53; }}
    blockquote p {{ margin:3px 0; }}
    .scan-summary {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px;
      margin:18px 0 22px; padding:16px; border:1px solid #dfe7f2; border-radius:14px;
      background:linear-gradient(135deg,#f8fbff,#f4f7fb); }}
    .summary-metric {{ min-width:0; padding:15px 17px; border-radius:11px; background:#fff;
      border:1px solid #e5eaf0; box-shadow:0 4px 12px rgba(16,42,67,.04); }}
    .summary-metric span {{ display:block; color:var(--muted); font-size:12px; font-weight:700; }}
    .summary-metric strong {{ display:inline-block; margin-top:3px; font-size:25px; line-height:1.1;
      letter-spacing:-.03em; font-variant-numeric:tabular-nums; }}
    .summary-metric em {{ margin-left:4px; color:var(--muted); font-size:12px; font-style:normal; }}
    .summary-metric.good strong {{ color:var(--green); }}
    .summary-metric.wait strong {{ color:var(--amber); }}
    .summary-metric.avoid strong {{ color:var(--red); }}
    .summary-note {{ grid-column:1/-1; margin:2px 2px 0; color:#536273; font-size:13px; }}
    .table-wrap {{ width:100%; margin:13px 0 24px; overflow:hidden; border:1px solid var(--line);
      border-radius:12px; background:#fff; box-shadow:0 5px 18px rgba(16,42,67,.045); }}
    .table-wrap.wide {{ overflow-x:auto; scrollbar-width:thin; scrollbar-color:#b9c3d0 transparent; }}
    table {{ width:100%; border-collapse:separate; border-spacing:0; table-layout:auto; font-size:15px;
      line-height:1.62; }}
    .table-wrap.wide table {{ min-width:1120px; }}
    th {{ position:sticky; top:0; z-index:1; color:#fff; background:var(--navy); font-weight:700;
      text-align:left; letter-spacing:-.01em; }}
    th,td {{ padding:15px 14px; border-bottom:1px solid var(--line); vertical-align:top;
      white-space:normal; overflow-wrap:break-word; word-break:keep-all; }}
    tbody tr:nth-child(even) {{ background:#f8fafc; }}
    tbody tr:hover {{ background:#f1f6fc; }}
    tbody tr:last-child td {{ border-bottom:0; }}
    td.cell-stock {{ min-width:165px; font-weight:700; color:#20334d; }}
    .stock-market {{ display:block; margin-top:4px; color:#667085; font-size:11.5px; font-weight:650; }}
    td.cell-market,td.cell-grade,td.cell-quality,td.cell-money {{ white-space:nowrap; }}
    td.cell-action {{ min-width:220px; }}
    td.cell-levels {{ min-width:190px; font-variant-numeric:tabular-nums; }}
    td.cell-history {{ min-width:200px; }}
    td.cell-range {{ min-width:210px; color:#526174; }}
    .grade-badge,.status-badge {{ display:inline-flex; align-items:center; justify-content:center;
      border-radius:999px; font-weight:750; white-space:nowrap; }}
    .grade-badge {{ min-width:34px; padding:3px 8px; font-size:12px; }}
    .grade-aplus,.grade-a {{ color:var(--green); background:var(--green-soft); border:1px solid #abefc6; }}
    .grade-b {{ color:#175cd3; background:#eff8ff; border:1px solid #b2ddff; }}
    .grade-c {{ color:var(--amber); background:var(--amber-soft); border:1px solid #fedf89; }}
    .grade-d {{ color:var(--red); background:var(--red-soft); border:1px solid #fecdca; }}
    .status-badge {{ margin:0 7px 4px 0; padding:3px 8px; font-size:11px; }}
    .status-good {{ color:var(--green); background:var(--green-soft); border:1px solid #abefc6; }}
    .status-wait {{ color:var(--amber); background:var(--amber-soft); border:1px solid #fedf89; }}
    .status-avoid {{ color:var(--red); background:var(--red-soft); border:1px solid #fecdca; }}
    .status-neutral {{ color:var(--slate); background:var(--slate-soft); border:1px solid #d0d5dd; }}
    .status-detail {{ display:block; margin-top:5px; color:#344054; line-height:1.5; }}
    .price-line {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px;
      padding:3px 0; white-space:nowrap; border-bottom:1px dashed #edf0f4; }}
    .price-line:last-child {{ border-bottom:0; }}
    .price-line span {{ color:#667085; font-size:12px; font-weight:650; }}
    .price-line strong {{ color:#1d2939; font-size:13px; font-weight:750; font-variant-numeric:tabular-nums; }}
    code {{ padding:2px 6px; border-radius:5px; background:#eef2f6; color:#334e68; }}
    strong {{ color:#0f3d75; }}
    .footer {{ margin-top:42px; padding-top:16px; border-top:1px solid #e8edf2;
      color:var(--muted); font-size:12px; text-align:right; }}
    @media (max-width:980px) {{
      main {{ width:calc(100% - 24px); margin:14px auto 34px; padding:30px 24px 34px; }}
      .table-wrap.wide table {{ min-width:1080px; }}
    }}
    @media (max-width:760px) {{
      body {{ background:#fff; }}
      main {{ width:100%; margin:0; padding:24px 16px 30px; border:0; border-radius:0; box-shadow:none; }}
      .top {{ height:5px; }} .report-kicker {{ margin-bottom:10px; }} h2 {{ margin-top:32px; }}
      .scan-summary {{ grid-template-columns:repeat(3,minmax(0,1fr)); gap:7px; padding:9px; }}
      .summary-metric {{ padding:11px 9px; }} .summary-metric strong {{ font-size:22px; }}
      .table-wrap {{ border:0; }} .table-wrap.wide table {{ min-width:0; }}
      table,tbody,tr,td {{ display:block; width:100%; }}
      thead {{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden;
        clip:rect(0,0,0,0); white-space:nowrap; border:0; }}
      tbody tr {{ margin:0 0 14px; padding:6px 12px; border:1px solid var(--line);
        border-radius:10px; background:#fff !important; box-shadow:0 3px 12px rgba(16,42,67,.05); }}
      tbody td {{ display:grid; grid-template-columns:minmax(92px,34%) 1fr; gap:10px;
        min-width:0 !important; padding:8px 2px; text-align:left !important;
        white-space:normal !important; border-bottom:1px solid var(--line); }}
      tbody td:last-child {{ border-bottom:0; }}
      tbody td::before {{ content:attr(data-label); color:var(--muted); font-weight:650; }}
    }}
    @media print {{ body {{ background:#fff; }} .top,.report-kicker {{ display:none; }} main {{ width:100%; margin:0;
      padding:0; border:0; box-shadow:none; }} th {{ position:static; }} .table-wrap {{ box-shadow:none; }} }}
  </style>
</head>
<body>
<div class="top"></div>
<main>
<div class="report-kicker">REAL MARKET ANALYSIS REPORT</div>
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
            f"| 분석 기준 종가 | {usd(daily.close)} | {daily.data_timestamp} 완료 일봉; 등급과 가격 기준의 계산값입니다. |",
            f"| 관찰 기준가 | {usd(daily.watch_price)} | {support_text} |",
            f"| 하락 경계가 | {usd(daily.invalidation_price)} | {invalidation_text} |",
            f"| 첫 저항가 | {usd(daily.first_target_price)} | {target_text} |",
            "",
            "## 거래량·큰 흐름",
            "",
            f"- 거래량: {_volume_text(daily.volume_ratio)}",
            f"- 변동성: ATR(14) {daily.atr14_pct:.2f}% · 분석 완료 캔들 {daily.candle_range_atr:.2f} ATR",
            f"- 추세 강도: ADX(14) {daily.adx14:.1f} · +DI {daily.plus_di14:.1f} / -DI {daily.minus_di14:.1f}",
            "- 거래량·기술지표는 분석 완료 일봉 기준이며 이후 현재가 변동을 반영하지 않습니다.",
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
        "> **가격 기준 읽는 법:** 관찰 기준 = 흐름을 확인할 가격 · 하락 경계 = 상승 시나리오가 깨지는 가격 · 첫 저항 = 위에서 막힐 수 있는 가격",
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
        "| 종목 | 등급 | 판단 | 가격 기준 | 과거 유사 패턴 | 평균 거래대금 | 기준 일봉 · 데이터 |",
        "|---|:---:|---|---|---|---:|---|",
    ]
    for item in items[:limit]:
        daily = item.daily
        similarity = item.similarity
        past = (
            f"{similarity.sample_count}건 중 {similarity.up_count}건 상승"
            if similarity.up_rate is not None
            else "표본 부족"
        )
        if similarity.sample_count and similarity.expected_return_pct is not None:
            past += f" · 평균 {similarity.expected_return_pct:+.2f}%"
        action = daily.action.replace("|", "/")
        if not item.liquid:
            action = f"유동성 부족: {item.liquidity_reason}"
        levels = (
            f"관찰 기준 {usd(daily.watch_price)} · "
            f"하락 경계 {usd(daily.invalidation_price)} · "
            f"첫 저항 {usd(daily.first_target_price)}"
        )
        cells = (
            f"{item.stock.display_name} ({item.stock.symbol})", daily.grade, action,
            levels, past, compact_money(daily.avg_trade_value_20),
            f"종가 {usd(daily.close)} · {daily.source_range}",
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


def _table_cell_class(header: str) -> str:
    normalized = " ".join(header.split())
    if normalized == "종목":
        return "cell-stock"
    if normalized == "시장":
        return "cell-market"
    if normalized == "등급":
        return "cell-grade"
    if "지금 할 일" in normalized or normalized == "판단":
        return "cell-action"
    if "손익비" in normalized or normalized == "ADX":
        return "cell-quality"
    if "확인/무효/목표" in normalized or "가격" == normalized or normalized == "가격 기준":
        return "cell-levels"
    if "과거" in normalized:
        return "cell-history"
    if "거래대금" in normalized:
        return "cell-money"
    if "데이터 범위" in normalized or "분석 일봉" in normalized or "기준 일봉" in normalized:
        return "cell-range"
    return ""


def _grade_html(text: str) -> str | None:
    value = text.strip().upper()
    mapping = {
        "A+": "grade-aplus",
        "A": "grade-a",
        "B": "grade-b",
        "C": "grade-c",
        "D": "grade-d",
    }
    css_class = mapping.get(value)
    if css_class is None:
        return None
    return f'<span class="grade-badge {css_class}">{escape(text.strip())}</span>'


def _status_html(text: str) -> str:
    cleaned = " ".join(text.split())
    statuses = (
        ("관심 후보", "status-good"),
        ("기다림", "status-wait"),
        ("피하기", "status-avoid"),
        ("유동성 부족", "status-avoid"),
    )
    for label, css_class in statuses:
        if cleaned.startswith(label):
            detail = cleaned[len(label) :].strip()
            detail = re.sub(r"^[-:·]\s*", "", detail)
            detail_html = (
                f'<span class="status-detail">{_inline_markdown(detail)}</span>' if detail else ""
            )
            return f'<span class="status-badge {css_class}">{escape(label)}</span>{detail_html}'
    return _inline_markdown(cleaned)


def _table_cell_html(header: str, text: str) -> str:
    normalized = " ".join(header.split())
    if normalized == "종목":
        market = re.fullmatch(r"(.+?)\s*·\s*(KOSPI|KOSDAQ)", text.strip())
        if market is not None:
            return (
                f'{_inline_markdown(market.group(1))}'
                f'<span class="stock-market">{escape(market.group(2))}</span>'
            )
    if normalized == "등급":
        grade = _grade_html(text)
        if grade is not None:
            return grade
    if "지금 할 일" in normalized or normalized == "판단":
        return _status_html(text)
    if normalized == "가격 기준":
        parts = [part.strip() for part in text.split("·") if part.strip()]
        labels = ("관찰 기준", "하락 경계", "첫 저항")
        rows: list[str] = []
        for part in parts:
            for label in labels:
                if part.startswith(label):
                    value = part[len(label) :].strip()
                    rows.append(
                        f'<div class="price-line"><span>{escape(label)}</span>'
                        f'<strong>{_inline_markdown(value)}</strong></div>'
                    )
                    break
            else:
                rows.append(f'<div class="price-line">{_inline_markdown(part)}</div>')
        if rows:
            return "".join(rows)
    return _inline_markdown(text)


def _scan_summary_html(quote_lines: list[str]) -> str | None:
    if not quote_lines:
        return None
    raw = re.sub(r"\*\*", "", quote_lines[0]).strip()
    match = re.fullmatch(
        r"결론:\s*관심 후보\s*(\d+)개\s*·\s*기다릴 종목\s*(\d+)개\s*·\s*피할 종목\s*(\d+)개",
        raw,
    )
    if match is None:
        return None
    interest, wait, avoid = match.groups()
    note = " ".join(quote_lines[1:]).strip()
    note_html = (
        f'<p class="summary-note">{_inline_markdown(note)}</p>'
        if note
        else ""
    )
    return (
        '<section class="scan-summary" aria-label="전체 분석 요약">'
        f'<div class="summary-metric good"><span>관심 후보</span><strong>{interest}</strong><em>종목</em></div>'
        f'<div class="summary-metric wait"><span>기다릴 종목</span><strong>{wait}</strong><em>종목</em></div>'
        f'<div class="summary-metric avoid"><span>피할 종목</span><strong>{avoid}</strong><em>종목</em></div>'
        f"{note_html}</section>"
    )


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
            scan_summary = _scan_summary_html(quote_lines)
            if scan_summary is not None:
                output.append(scan_summary)
                continue
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
                    f'<td class="{_table_cell_class(headers[position] if position < len(headers) else "")}" '
                    f'data-label="{escape(headers[position] if position < len(headers) else "")}">'
                    f"{_table_cell_html(headers[position] if position < len(headers) else '', cell)}</td>"
                    for position, cell in enumerate(row)
                )
                + "</tr>"
                for row in rows
            )
            wrapper_class = "table-wrap wide" if len(headers) >= 6 else "table-wrap"
            output.append(
                f'<div class="{wrapper_class}"><table><thead><tr>{head}</tr></thead>'
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
