from __future__ import annotations

"""Interactive Kiwoom REST analyzer for liquid KOSPI/KOSDAQ common stocks."""

import argparse
import ctypes
import json
import os
import re
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd

from core.credentials import CredentialError, CredentialStore, KiwoomCredentials
from core.domestic_kiwoom_rest import (
    KOREA,
    DomesticKiwoomRestClient,
    DomesticQuote,
    InsufficientDomesticHistoryError,
    is_supported_domestic_stock,
)
from core.domestic_reporting import (
    render_domestic_individual_report,
    render_domestic_scan_report,
)
from core.domestic_storage import DomesticCacheStore
from core.domestic_universe import DomesticUniverseCandidate, build_domestic_scan_universe
from core.ichimoku import MIN_BARS, IchimokuReading, analyze_ichimoku, resample_weekly
from core.kiwoom_rest import InvalidSecurityError, KiwoomRestError, StockInfo
from core.reporting import AnalyzedStock, render_html_report
from core.similarity import summarize_similarity


ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / ".runtime"
CACHE_DIR = Path(os.getenv("REAL_KR_CACHE_DIR", ROOT / ".domestic_ichimoku_cache"))
REPORTS_DIR = Path(os.getenv("REAL_KR_REPORTS_DIR", ROOT / "reports"))
CREDENTIAL_PATH = RUNTIME_DIR / "kiwoom_rest_credentials.dat"
LOCK_PATH = RUNTIME_DIR / "analyzer.lock"
SCAN_SIZE = max(50, min(400, int(os.getenv("REAL_KR_SCAN_SIZE", "160"))))
SCAN_WORKERS = max(1, min(12, int(os.getenv("REAL_KR_SCAN_WORKERS", "6"))))
MIN_AVG_VOLUME = max(0.0, float(os.getenv("REAL_KR_MIN_AVG_VOLUME", "100000")))
MIN_AVG_TRADE_VALUE = max(
    0.0,
    float(os.getenv("REAL_KR_MIN_AVG_TRADE_VALUE", "5000000000")),
)
CODE_RE = re.compile(r"^[0-9A-Z]{6}$", re.IGNORECASE)


class IncompleteDomesticScanError(KiwoomRestError):
    """The report was saved, but at least one selected stock failed analysis."""


class DomesticStockAnalyzer:
    def __init__(self, client: DomesticKiwoomRestClient, cache: DomesticCacheStore) -> None:
        self.client = client
        self.cache = cache
        self._master: list[StockInfo] | None = None

    def master(self, *, allow_network: bool = True) -> list[StockInfo]:
        if self._master is not None:
            return self._master
        cached = self.cache.load_master()
        if cached is not None:
            self._master = cached
            return cached
        stale = self.cache.load_master(max_age_hours=None)
        if not allow_network:
            return stale or []
        print("[준비] KOSPI·KOSDAQ 일반주 목록을 키움에서 받아옵니다...")
        try:
            stocks = self.client.stock_master()
        except KiwoomRestError:
            if stale:
                print("[주의] 최신 국내 종목목록 갱신에 실패해 이전 저장 목록을 사용합니다.")
                self._master = stale
                return stale
            raise
        if not stocks:
            if stale:
                print("[주의] 최신 국내 종목목록이 비어 있어 이전 저장 목록을 사용합니다.")
                self._master = stale
                return stale
            raise KiwoomRestError("국내 일반주 종목 목록을 만들지 못했습니다.")
        self.cache.save_master(stocks)
        self._master = stocks
        print(f"[준비 완료] 국내 일반주 {len(stocks):,}개를 확인했습니다.")
        return stocks

    def resolve_stock(self, text: str) -> StockInfo:
        query = _command_query(text)
        code_query = query.upper()
        master = self.master()
        if CODE_RE.fullmatch(code_query):
            choices = [item for item in master if item.symbol.upper() == code_query]
            if not choices:
                raise InvalidSecurityError(
                    f"'{code_query}'에 해당하는 KOSPI·KOSDAQ 일반주를 찾지 못했습니다."
                )
            return choices[0]

        folded = query.casefold()
        exact = [item for item in master if item.display_name.casefold() == folded]
        partial = [item for item in master if folded in item.display_name.casefold()]
        choices = exact or partial
        if not choices:
            raise InvalidSecurityError(f"'{query}'에 해당하는 국내 일반주를 찾지 못했습니다.")
        if len(choices) > 1:
            names = ", ".join(f"{item.display_name}({item.symbol})" for item in choices[:10])
            raise InvalidSecurityError(f"종목이 여러 개입니다. 6자리 종목코드로 입력해 주세요: {names}")
        return choices[0]

    def market_context(self) -> tuple[str, bool]:
        readings: list[IchimokuReading] = []
        labels: list[str] = []
        for code, label in (("001", "KOSPI"), ("101", "KOSDAQ")):
            try:
                frame = self.client.index_daily_bars(code)
                readings.append(analyze_ichimoku(frame, timeframe=f"{label} 일봉"))
                labels.append(label)
            except Exception:
                continue
        if len(readings) < 2:
            return "KOSPI·KOSDAQ 일부 확인 불가", False
        strong = sum(
            item.price_position == "구름 위" and item.grade in {"A+", "A"}
            for item in readings
        )
        weak = sum(item.price_position == "구름 아래" or item.grade == "D" for item in readings)
        if strong == 2:
            return "KOSPI·KOSDAQ 모두 상승 방향", False
        if weak == 2:
            return "KOSPI·KOSDAQ 모두 약세", True
        return "KOSPI·KOSDAQ 방향이 엇갈림", False

    def analyze_individual(self, text: str) -> tuple[AnalyzedStock, Path]:
        stock = self.resolve_stock(text)
        market_label, market_weak = self.market_context()
        result = self._analyze_stock(
            stock,
            market_label=market_label,
            market_weak=market_weak,
            include_quote=True,
            include_intraday=True,
        )
        now = datetime.now(KOREA)
        folder = REPORTS_DIR / f"국내_{stock.symbol}_{now:%Y%m%d_%H%M%S}"
        folder.mkdir(parents=True, exist_ok=True)
        markdown = render_domestic_individual_report(result)
        md_path = folder / "report.md"
        md_path.write_text(markdown, encoding="utf-8")
        html_path = folder / "report.html"
        html_path.write_text(
            render_html_report(
                markdown,
                title=f"{stock.display_name} ({stock.symbol}) 국내주식 분석",
                footer="Real · 키움 REST API 국내주식 · 일목 9·26·52 · ATR/ADX",
            ),
            encoding="utf-8",
        )
        return result, html_path

    def analyze_all(self) -> tuple[list[AnalyzedStock], list[str], Path, dict[str, int]]:
        started = datetime.now(KOREA)
        print("[국내 전체 분석] KOSPI·KOSDAQ 시장과 종목 목록을 준비합니다.")
        master = self.master()
        market_label, market_weak = self.market_context()
        print(f"[시장 확인] {market_label}")
        candidates, stats = build_domestic_scan_universe(
            self.client,
            master,
            target_size=SCAN_SIZE,
            per_sector_limit=max(16, SCAN_SIZE // 8),
        )
        if not candidates:
            raise KiwoomRestError("국내 전체 분석 후보를 만들지 못했습니다.")
        total = len(candidates)
        print(
            f"[선별 완료] KOSPI·KOSDAQ 거래대금 상위 일반주 {total:,}개를 "
            "완료 일봉 기준으로 끝까지 분석합니다."
        )

        results: list[AnalyzedStock] = []
        pending: list[tuple[DomesticUniverseCandidate, str]] = []
        insufficient_history: list[str] = []
        completed = 0
        started_mono = time.monotonic()

        def work(candidate: DomesticUniverseCandidate) -> AnalyzedStock:
            return self._analyze_stock(
                candidate.stock,
                market_label=market_label,
                market_weak=market_weak,
                include_quote=False,
                include_intraday=False,
            )

        with ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="kr-scan") as executor:
            futures: dict[Future[AnalyzedStock], DomesticUniverseCandidate] = {
                executor.submit(work, candidate): candidate for candidate in candidates
            }
            for future in as_completed(futures):
                candidate = futures[future]
                completed += 1
                try:
                    results.append(future.result())
                except InsufficientDomesticHistoryError as exc:
                    insufficient_history.append(
                        f"{candidate.stock.display_name} ({candidate.stock.symbol}): {_short_error(exc)}"
                    )
                except Exception as exc:
                    pending.append((candidate, _short_error(exc)))
                if completed == 1 or completed % 10 == 0 or completed == total:
                    elapsed = max(0.1, time.monotonic() - started_mono)
                    rate = completed / elapsed
                    remaining = (total - completed) / rate if rate > 0 else 0.0
                    print(
                        f"[국내 전체 분석] {completed:,}/{total:,} ({completed / total * 100:.1f}%) "
                        f"· 예상 남은 시간 약 {int(remaining // 60)}분 {int(remaining % 60)}초"
                    )

        if pending:
            print(f"[자동 복구] 일시 오류 {len(pending)}개를 다시 확인합니다.")
        failures: list[str] = []
        for index, (candidate, previous_error) in enumerate(pending, start=1):
            try:
                results.append(work(candidate))
            except InsufficientDomesticHistoryError as exc:
                insufficient_history.append(
                    f"{candidate.stock.display_name} ({candidate.stock.symbol}): {_short_error(exc)}"
                )
            except Exception as exc:
                failures.append(
                    f"{candidate.stock.display_name} ({candidate.stock.symbol}): "
                    f"{_short_error(exc)} / 최초 오류: {previous_error}"
                )
            if index % 10 == 0 or index == len(pending):
                print(f"[자동 복구] {index}/{len(pending)} 재확인 완료")

        stats["insufficient_history"] = len(insufficient_history)
        if insufficient_history:
            print(
                f"[자동 제외] 신규상장·데이터 기간 부족 {len(insufficient_history)}개는 "
                "일목 분석 대상에서 제외했습니다."
            )

        finished = datetime.now(KOREA)
        folder = REPORTS_DIR / f"전체_국내주식_{finished:%Y%m%d_%H%M%S}"
        folder.mkdir(parents=True, exist_ok=True)
        markdown = render_domestic_scan_report(
            results,
            started_at=started,
            finished_at=finished,
            universe_stats=stats,
            failures=failures,
        )
        md_path = folder / "report.md"
        md_path.write_text(markdown, encoding="utf-8")
        report_path = folder / "report.html"
        report_path.write_text(
            render_html_report(
                markdown,
                title="국내주식 전체 분석",
                footer="Real · 키움 REST API 국내주식 · KOSPI/KOSDAQ · ATR/ADX",
            ),
            encoding="utf-8",
        )
        _write_scan_csv(folder / "all_results.csv", results)
        if failures:
            (folder / "failures.json").write_text(
                json.dumps(failures, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if insufficient_history:
            (folder / "insufficient_history.json").write_text(
                json.dumps(insufficient_history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        self.cache.save_scan_state(
            {
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "selected": total,
                "analyzed": len(results),
                "failures": failures,
                "report": str(report_path),
                "markdown_report": str(md_path),
            }
        )
        return results, failures, report_path, stats

    def _analyze_stock(
        self,
        stock: StockInfo,
        *,
        market_label: str,
        market_weak: bool,
        include_quote: bool,
        include_intraday: bool,
    ) -> AnalyzedStock:
        if not is_supported_domestic_stock(stock):
            raise InvalidSecurityError(
                f"{stock.symbol}은(는) 국내 일반주 분석 대상이 아닙니다."
            )
        daily_frame = self._daily(stock)
        daily = analyze_ichimoku(daily_frame, timeframe="일봉")
        weekly: IchimokuReading | None = None
        try:
            weekly = analyze_ichimoku(resample_weekly(daily_frame), timeframe="주봉")
        except (ValueError, IndexError):
            weekly = None
        daily = _apply_domestic_context(
            daily,
            weekly=weekly,
            market_label=market_label,
            market_is_weak=market_weak,
        )
        similarity = summarize_similarity(daily_frame)

        quote: DomesticQuote | None = self.client.quote(stock) if include_quote else None
        intraday: IchimokuReading | None = None
        if include_intraday:
            try:
                intraday = analyze_ichimoku(self._minute(stock, interval=60), timeframe="60분봉")
            except (KiwoomRestError, ValueError, IndexError):
                intraday = None

        liquid, reason = _liquidity(daily)
        if not liquid:
            blocks = tuple(dict.fromkeys((*daily.hard_blocks, reason)))
            risks = tuple(dict.fromkeys((*daily.risks, reason)))
            daily = replace(
                daily,
                hard_blocks=blocks,
                risks=risks,
                risk_score=len(risks) + len(blocks) * 2,
                confidence="보통" if daily.confidence == "높음" else daily.confidence,
                action="피하기 - 거래량·거래대금이 부족해 매매 후보에서 제외",
            )
        return AnalyzedStock(
            stock=stock,
            daily=daily,
            similarity=similarity,
            quote=quote,  # type: ignore[arg-type]
            intraday=intraday,
            liquid=liquid,
            liquidity_reason=reason,
        )

    def _daily(self, stock: StockInfo) -> pd.DataFrame:
        cached = self.cache.load_daily(stock, fresh_only=True)
        if cached is not None and len(cached) >= MIN_BARS:
            return cached.tail(650)
        frame = self.client.daily_bars(stock, max_rows=650)
        self.cache.save_daily(stock, frame)
        return frame

    def _minute(self, stock: StockInfo, *, interval: int) -> pd.DataFrame:
        cached = self.cache.load_minute(stock, interval, fresh_only=True)
        if cached is not None and len(cached) >= 80:
            return cached
        frame = self.client.minute_bars(stock, interval_minutes=interval, max_rows=500)
        self.cache.save_minute(stock, interval, frame)
        return frame


def _apply_domestic_context(
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
    else:
        blocks.append("상위 시간대인 주봉을 확인할 데이터가 부족합니다.")
        risks.append("주봉 방향을 확인하지 못해 일봉 단독 신호로 판단하지 않습니다.")
    if market_is_weak:
        blocks.append("KOSPI와 KOSDAQ 시장 흐름이 모두 약합니다.")
        risks.append("종목 차트가 좋아도 국내 시장 동반 하락의 영향을 받을 수 있습니다.")
    elif "확인 불가" in market_label:
        blocks.append("KOSPI·KOSDAQ 시장 흐름을 충분히 확인하지 못했습니다.")
        risks.append("시장 지수 데이터가 불완전해 종목 단독 신호를 확정하지 않습니다.")
    elif "모두 상승" not in market_label:
        # Mixed KOSPI/KOSDAQ is a risk penalty, not a hard block. This avoids
        # discarding a strong stock merely because the other board is weak.
        risks.append("KOSPI와 KOSDAQ의 방향이 함께 상승으로 정렬되지 않았습니다.")
        if reading.grade in {"A+", "A", "B"}:
            action = "기다림 - 국내 시장 방향이 함께 좋아질 때 재확인"
    if blocks and reading.grade in {"A+", "A", "B"}:
        action = "기다림 - 종목·주봉·시장 방향이 함께 좋아질 때 재확인"
    blocks = list(dict.fromkeys(blocks))
    risks = list(dict.fromkeys(risks))
    confidence = reading.confidence
    if confidence == "높음" and (blocks or higher != "주봉도 상승 방향" or "모두 상승" not in market_label):
        confidence = "보통"
    return replace(
        reading,
        hard_blocks=tuple(blocks),
        risks=tuple(risks),
        risk_score=len(risks) + len(blocks) * 2,
        confidence=confidence,
        action=action,
        higher_timeframe=higher,
        market_context=market_label,
    )


def _liquidity(reading: IchimokuReading) -> tuple[bool, str]:
    value = reading.avg_trade_value_20
    volume = reading.avg_volume_20
    if value is None or value < MIN_AVG_TRADE_VALUE:
        actual = (value or 0.0) / 100_000_000
        required = MIN_AVG_TRADE_VALUE / 100_000_000
        return False, f"최근 20일 평균 거래대금 {actual:.1f}억원 (기준 {required:.0f}억원 이상)"
    if volume is None or volume < MIN_AVG_VOLUME:
        return False, (
            f"최근 20일 평균 거래량 {(volume or 0):,.0f}주 "
            f"(기준 {MIN_AVG_VOLUME:,.0f}주 이상)"
        )
    return True, ""


def _command_query(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(
        r"\s*(국내\s*)?(주식\s*)?분석\s*(해줘|해주세요|해|)?\s*$",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = cleaned.strip(" `\"'")
    if not cleaned:
        raise InvalidSecurityError("종목명이나 6자리 종목코드를 입력해 주세요. 예: 삼성전자 분석해줘")
    return cleaned


def _short_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:220] or exc.__class__.__name__


def _write_scan_csv(path: Path, results: list[AnalyzedStock]) -> None:
    rows: list[dict[str, object]] = []
    for item in results:
        daily = item.daily
        similarity = item.similarity
        rows.append(
            {
                "symbol": item.stock.symbol,
                "name": item.stock.display_name,
                "market": item.stock.exchange,
                "sector": item.stock.sector,
                "grade": daily.grade,
                "action": daily.action,
                "price_krw": daily.close,
                "watch_price_krw": daily.watch_price,
                "invalidation_price_krw": daily.invalidation_price,
                "first_target_price_krw": daily.first_target_price,
                "reward_risk_ratio": daily.reward_risk_ratio,
                "adx14": daily.adx14,
                "plus_di14": daily.plus_di14,
                "minus_di14": daily.minus_di14,
                "atr14_pct": daily.atr14_pct,
                "volume_ratio": daily.volume_ratio,
                "avg_volume_20": daily.avg_volume_20,
                "avg_trade_value_20_krw": daily.avg_trade_value_20,
                "liquid": item.liquid,
                "hard_blocks": " / ".join(daily.hard_blocks),
                "similar_up_count": similarity.up_count,
                "similar_sample_count": similarity.sample_count,
                "similar_up_rate": similarity.up_rate,
                "similar_expected_return_pct": similarity.expected_return_pct,
                "similar_downside_p25_pct": similarity.downside_p25_pct,
                "similar_payoff_ratio": similarity.payoff_ratio,
                "similar_invalidation_rate": similarity.invalidation_rate,
                "weekly_context": daily.higher_timeframe,
                "market_context": daily.market_context,
                "timeframe": daily.timeframe,
                "ichimoku_parameters": "9,26,52",
                "data_source": "키움 REST API 국내주식 ka10081 수정주가 일봉",
                "timezone": "Asia/Seoul",
                "data_timestamp": daily.data_timestamp,
                "source_range": daily.source_range,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _is_process_alive(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    ctypes.windll.kernel32.CloseHandle(process)
    return True


@contextmanager
def single_instance() -> Iterator[None]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            pid = int(LOCK_PATH.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pid = 0
        if _is_process_alive(pid):
            raise RuntimeError("다른 키움 주식 분석이 이미 실행 중입니다. 기존 작업이 끝난 뒤 다시 실행해 주세요.")
        LOCK_PATH.unlink(missing_ok=True)
    handle = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(handle, str(os.getpid()).encode("ascii"))
        os.close(handle)
        yield
    finally:
        try:
            os.close(handle)
        except OSError:
            pass
        LOCK_PATH.unlink(missing_ok=True)


def _credentials(store: CredentialStore, *, configure: bool) -> KiwoomCredentials:
    if configure:
        return store.configure_interactively()
    loaded = store.load()
    return loaded or store.configure_interactively()


def _self_test() -> int:
    rng = np.random.default_rng(77)
    dates = pd.bdate_range("2023-01-02", periods=560)
    close = 70_000 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, len(dates))))
    frame = pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, len(dates))),
            "high": close * (1 + rng.uniform(0.002, 0.018, len(dates))),
            "low": close * (1 - rng.uniform(0.002, 0.018, len(dates))),
            "close": close,
            "volume": rng.integers(200_000, 4_000_000, len(dates)),
        },
        index=dates,
    )
    frame["high"] = frame[["high", "open", "close"]].max(axis=1)
    frame["low"] = frame[["low", "open", "close"]].min(axis=1)
    frame["trade_value"] = frame["close"] * frame["volume"]
    reading = analyze_ichimoku(frame)
    similarity = summarize_similarity(frame)
    assert reading.close > 0 and reading.atr14 > 0
    assert 0 <= similarity.sample_count <= 30
    print("DOMESTIC_SELF_TEST_OK")
    return 0


def _print_help() -> None:
    print("\n사용 가능한 국내주식 명령")
    print("  개별 종목 분석 : 삼성전자 분석해줘 / 005930 분석해줘")
    print("  전체 종목 분석 : 국내 전체 분석해줘")
    print("  프로그램 종료  : 종료")
    print("\n우선주·스팩·관리/경고 종목 등은 전체 분석 후보에서 제외됩니다.\n")


def run_command(analyzer: DomesticStockAnalyzer, command: str) -> bool:
    normalized = re.sub(r"\s+", " ", command.strip()).casefold()
    if normalized in {"종료", "q", "quit", "exit"}:
        return False
    if normalized in {"도움말", "명령어", "?", "help"}:
        _print_help()
        return True
    if normalized in {
        "전체 분석해줘",
        "전체분석해줘",
        "국내 전체 분석해줘",
        "국내주식 전체 분석해줘",
        "국내 주식 전체 분석해줘",
    }:
        results, failures, path, _ = analyzer.analyze_all()
        interest_count = sum(
            item.liquid
            and item.daily.grade in {"A+", "A"}
            and not item.daily.hard_blocks
            and item.daily.higher_timeframe == "주봉도 상승 방향"
            and "모두 상승" in item.daily.market_context
            for item in results
        )
        print(
            f"\n완료: {len(results):,}개 분석 · 관심 후보 {interest_count:,}개 · 오류 {len(failures):,}개"
        )
        print(f"HTML 보고서: {path}")
        print(f"Markdown 보고서: {path.with_suffix('.md')}\n")
        if failures:
            raise IncompleteDomesticScanError(
                f"국내 전체 분석 중 {len(failures):,}개 종목에 실패했습니다. 저장된 보고서에서 오류를 확인해 주세요."
            )
        return True
    if "분석" in normalized:
        result, path = analyzer.analyze_individual(command)
        similarity = result.similarity
        past = (
            f"{similarity.up_count}/{similarity.sample_count} 상승"
            if similarity.sample_count
            else "유사 과거 표본 부족"
        )
        live_price = result.quote.price if result.quote is not None else result.daily.close
        print(
            f"\n{result.stock.display_name} ({result.stock.symbol}) | {result.daily.grade} | "
            f"{result.daily.action} | {past}"
        )
        # Use plain Korean currency text in the console.  The Unicode won sign
        # (₩) cannot be encoded by some default Windows cp949 shells and used
        # to crash an otherwise successful analysis at the final print step.
        print(f"현재가: {live_price:,.0f}원")
        target = result.daily.first_target_price
        target_text = "확인 불가" if target is None else f"{target:,.0f}원"
        print(
            f"확인 가격 {result.daily.watch_price:,.0f}원 · "
            f"시나리오 무효 {result.daily.invalidation_price:,.0f}원 · "
            f"첫 목표 후보 {target_text}"
        )
        if result.daily.hard_blocks:
            print("기다리는 이유: " + " / ".join(result.daily.hard_blocks[:3]))
        print(f"HTML 보고서: {path}")
        print(f"Markdown 보고서: {path.with_suffix('.md')}\n")
        return True
    print("명령을 이해하지 못했습니다. '도움말'을 입력해 주세요.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="키움 REST 국내주식 일목균형표 분석기")
    parser.add_argument("--configure", action="store_true", help="암호화 저장된 키움 API 키 다시 설정")
    parser.add_argument("--once", default="", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    try:
        credentials = _credentials(CredentialStore(CREDENTIAL_PATH), configure=args.configure)
        if args.configure:
            print("\n키움 REST API 키를 안전하게 다시 저장했습니다.")
            return 0
        client = DomesticKiwoomRestClient(credentials)
        analyzer = DomesticStockAnalyzer(client, DomesticCacheStore(CACHE_DIR))
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        with single_instance():
            print("=" * 46)
            print(" Real 국내주식 일목균형표 분석기")
            print("=" * 46)
            print("데이터: 키움 REST API 국내주식 / 통화: KRW")
            _print_help()
            if args.once:
                run_command(analyzer, args.once)
                return 0
            while True:
                try:
                    command = input("명령 입력: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n종료합니다.")
                    return 0
                if command and not run_command(analyzer, command):
                    print("종료합니다.")
                    return 0
    except IncompleteDomesticScanError as exc:
        print(f"\n일부 실패: {exc}", file=sys.stderr)
        return 2
    except (CredentialError, KiwoomRestError, InvalidSecurityError, RuntimeError, ValueError) as exc:
        print(f"\n오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
