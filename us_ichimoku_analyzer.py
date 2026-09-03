from __future__ import annotations

"""Interactive Kiwoom REST Ichimoku analyzer for liquid US common stocks."""

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
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from core.credentials import CredentialError, CredentialStore, KiwoomCredentials
from core.ichimoku import IchimokuReading, analyze_ichimoku, apply_context, resample_weekly
from core.kiwoom_rest import (
    InvalidSecurityError,
    KiwoomRestClient,
    KiwoomRestError,
    Quote,
    StockInfo,
    regular_session_minute_bars,
)
from core.reporting import (
    AnalyzedStock,
    one_line,
    render_html_report,
    render_individual_report,
    render_scan_report,
    usd,
)
from core.similarity import summarize_similarity
from core.storage import CacheStore
from core.universe import UniverseCandidate, build_scan_universe, is_supported_common_stock


ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / ".runtime"
CACHE_DIR = Path(os.getenv("REAL2_CACHE_DIR", ROOT / ".us_ichimoku_cache"))
REPORTS_DIR = Path(os.getenv("REAL2_REPORTS_DIR", ROOT / "reports"))
CREDENTIAL_PATH = RUNTIME_DIR / "kiwoom_rest_credentials.dat"
LOCK_PATH = RUNTIME_DIR / "analyzer.lock"
US_EASTERN = ZoneInfo("America/New_York")
SCAN_SIZE = max(50, min(500, int(os.getenv("REAL2_SCAN_SIZE", "220"))))
SCAN_WORKERS = max(1, min(16, int(os.getenv("REAL2_SCAN_WORKERS", "8"))))
MIN_AVG_VOLUME = max(0.0, float(os.getenv("REAL2_MIN_AVG_VOLUME", "200000")))
MIN_AVG_DOLLAR_VOLUME = max(
    0.0, float(os.getenv("REAL2_MIN_AVG_DOLLAR_VOLUME", "20000000"))
)
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")


class USStockAnalyzer:
    def __init__(self, client: KiwoomRestClient, cache: CacheStore) -> None:
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
        if not allow_network:
            return []
        print("[준비] 미국 일반주 목록을 키움에서 한 번만 받아옵니다...")
        stocks = self.client.stock_master("%")
        self.cache.save_master(stocks)
        self._master = stocks
        print(f"[준비 완료] 미국 종목 {len(stocks):,}개를 확인했습니다.")
        return stocks

    def resolve_stock(self, text: str) -> StockInfo:
        query = _command_query(text)
        normalized = query.strip().upper()
        master = self.master()
        if SYMBOL_RE.fullmatch(normalized):
            matches = _unique_stocks(
                stock for stock in master if stock.symbol.upper() == normalized
            )
            matches = [stock for stock in matches if is_supported_common_stock(stock)]
            if not matches:
                raise InvalidSecurityError(
                    f"'{normalized}'에 해당하는 미국 일반주를 종목 목록에서 찾지 못했습니다."
                )
            if len(matches) > 1:
                exchanges = ", ".join(stock.exchange for stock in matches)
                raise InvalidSecurityError(
                    f"{normalized}이 여러 거래소에 있어 하나로 확정할 수 없습니다: {exchanges}"
                )
            return matches[0]
        exact = [
            stock
            for stock in master
            if query.casefold()
            in {
                stock.korean_name.casefold(),
                stock.english_name.casefold(),
                stock.symbol.casefold(),
            }
        ]
        partial = [
            stock
            for stock in master
            if query.casefold() in stock.korean_name.casefold()
            or query.casefold() in stock.english_name.casefold()
        ]
        choices = _unique_stocks(exact or partial)
        choices = [stock for stock in choices if is_supported_common_stock(stock)]
        if not choices:
            raise InvalidSecurityError(f"'{query}'에 해당하는 미국 일반주를 찾지 못했습니다.")
        if len(choices) > 1:
            names = ", ".join(f"{item.display_name}({item.symbol})" for item in choices[:8])
            raise InvalidSecurityError(f"종목이 여러 개입니다. 티커로 입력해 주세요: {names}")
        return choices[0]

    def market_context(self) -> tuple[str, bool]:
        defaults = {
            "SPY": StockInfo("SPY", "NY", "S&P 500 시장", "SPDR S&P 500 ETF", "시장", True),
            "QQQ": StockInfo("QQQ", "ND", "나스닥 100 시장", "Invesco QQQ", "시장", True),
        }
        known = self._master or self.cache.load_master() or []
        known_by_symbol = {item.symbol: item for item in known}
        proxies: list[StockInfo] = []
        for symbol, default in defaults.items():
            stock = known_by_symbol.get(symbol)
            if stock is None:
                try:
                    stock = self.client.stock_info(symbol, default.exchange)
                except Exception:
                    stock = default
            proxies.append(stock)
        readings: list[IchimokuReading] = []
        failures: list[str] = []
        for stock in proxies:
            try:
                frame = self._daily(stock, full_history=False)
                readings.append(analyze_ichimoku(frame, timeframe=f"{stock.symbol} 일봉"))
            except Exception as exc:
                failures.append(f"{stock.symbol}: {_short_error(exc)}")
        if len(readings) < 2:
            return "SPY·QQQ 일부 확인 불가", False
        strong = sum(item.price_position == "구름 위" and item.grade in {"A+", "A"} for item in readings)
        weak = sum(item.price_position == "구름 아래" or item.grade == "D" for item in readings)
        if strong == 2:
            return "SPY·QQQ 모두 상승 방향", False
        if weak == 2:
            return "SPY·QQQ 모두 약세", True
        return "SPY·QQQ 방향이 엇갈림", False

    def analyze_individual(self, text: str) -> tuple[AnalyzedStock, Path]:
        stock = self.resolve_stock(text)
        market_label, market_weak = self.market_context()
        result = self._analyze_stock(
            stock,
            market_label=market_label,
            market_weak=market_weak,
            include_quote=True,
            include_intraday=True,
            full_history=True,
        )
        now = datetime.now(US_EASTERN)
        folder = REPORTS_DIR / f"{stock.symbol}_{now:%Y%m%d_%H%M%S}"
        folder.mkdir(parents=True, exist_ok=True)
        markdown = render_individual_report(result)
        (folder / "report.md").write_text(markdown, encoding="utf-8")
        html_path = folder / "report.html"
        html_path.write_text(
            render_html_report(markdown, title=f"{stock.display_name} ({stock.symbol}) 미국주식 분석"),
            encoding="utf-8",
        )
        return result, html_path

    def analyze_all(self) -> tuple[list[AnalyzedStock], list[str], Path, dict[str, int]]:
        started = datetime.now(US_EASTERN)
        print("[전체 분석] 미국 시장과 종목 목록을 준비합니다.")
        master = self.master()
        market_label, market_weak = self.market_context()
        print(f"[시장 확인] {market_label}")
        candidates, stats = build_scan_universe(
            self.client,
            master,
            target_size=SCAN_SIZE,
            per_sector_limit=max(12, SCAN_SIZE // 10),
        )
        total = len(candidates)
        print(
            f"[선별 완료] 시가총액·거래대금 상위 일반주 {total:,}개를 "
            "한 번의 명령으로 끝까지 분석합니다."
        )
        if not candidates:
            raise KiwoomRestError("전체 분석 후보를 만들지 못했습니다.")

        results: list[AnalyzedStock] = []
        pending: list[tuple[UniverseCandidate, str]] = []
        completed = 0
        started_mono = time.monotonic()

        def work(candidate: UniverseCandidate) -> AnalyzedStock:
            return self._analyze_stock(
                candidate.stock,
                market_label=market_label,
                market_weak=market_weak,
                include_quote=False,
                include_intraday=False,
                full_history=False,
            )

        with ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="us-scan") as executor:
            futures: dict[Future[AnalyzedStock], UniverseCandidate] = {
                executor.submit(work, candidate): candidate for candidate in candidates
            }
            for future in as_completed(futures):
                candidate = futures[future]
                completed += 1
                try:
                    results.append(future.result())
                except Exception as exc:
                    pending.append((candidate, _short_error(exc)))
                if completed == 1 or completed % 10 == 0 or completed == total:
                    elapsed = max(0.1, time.monotonic() - started_mono)
                    rate = completed / elapsed
                    remaining = (total - completed) / rate if rate > 0 else 0
                    print(
                        f"[전체 분석] {completed:,}/{total:,} ({completed / total * 100:.1f}%) "
                        f"· 예상 남은 시간 약 {int(remaining // 60)}분 {int(remaining % 60)}초"
                    )

        if pending:
            print(f"[자동 복구] 일시 오류 {len(pending)}개를 다시 확인합니다.")
        final_failures: list[str] = []
        for index, (candidate, previous_error) in enumerate(pending, start=1):
            try:
                results.append(work(candidate))
            except Exception as exc:
                final_failures.append(
                    f"{candidate.stock.display_name} ({candidate.stock.symbol}): "
                    f"{_short_error(exc)} / 최초 오류: {previous_error}"
                )
            if index % 10 == 0 or index == len(pending):
                print(f"[자동 복구] {index}/{len(pending)} 재확인 완료")

        finished = datetime.now(US_EASTERN)
        folder = REPORTS_DIR / f"전체_미국주식_{finished:%Y%m%d_%H%M%S}"
        folder.mkdir(parents=True, exist_ok=True)
        markdown = render_scan_report(
            results,
            started_at=started,
            finished_at=finished,
            universe_stats=stats,
            failures=final_failures,
        )
        (folder / "report.md").write_text(markdown, encoding="utf-8")
        report_path = folder / "report.html"
        report_path.write_text(
            render_html_report(markdown, title="미국주식 전체 분석"), encoding="utf-8"
        )
        _write_scan_csv(folder / "all_results.csv", results)
        if final_failures:
            (folder / "failures.json").write_text(
                json.dumps(final_failures, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        self.cache.save_scan_state(
            {
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "selected": total,
                "analyzed": len(results),
                "failures": final_failures,
                "report": str(report_path),
                "markdown_report": str(folder / "report.md"),
            }
        )
        return results, final_failures, report_path, stats

    def _analyze_stock(
        self,
        stock: StockInfo,
        *,
        market_label: str,
        market_weak: bool,
        include_quote: bool,
        include_intraday: bool,
        full_history: bool,
    ) -> AnalyzedStock:
        self._validate_stock(stock, allow_market_proxy=stock.symbol in {"SPY", "QQQ"})
        daily_frame = self._daily(stock, full_history=full_history)
        quote: Quote | None = None
        if include_quote:
            quote = self.client.quote(stock)
        # The quote is displayed as live context only.  It must never replace
        # the latest completed daily close used by Ichimoku and similarity.
        analysis_frame = daily_frame
        daily = analyze_ichimoku(analysis_frame, timeframe="일봉")
        weekly: IchimokuReading | None = None
        try:
            weekly_frame = resample_weekly(analysis_frame)
            weekly = analyze_ichimoku(weekly_frame, timeframe="주봉")
        except (ValueError, IndexError):
            weekly = None
        daily = apply_context(
            daily,
            weekly=weekly,
            market_label=market_label,
            market_is_weak=market_weak,
        )
        similarity = summarize_similarity(analysis_frame)
        intraday: IchimokuReading | None = None
        if include_intraday:
            try:
                minute = self._minute(stock, interval=60)
                intraday = analyze_ichimoku(minute, timeframe="60분봉")
            except (KiwoomRestError, ValueError, IndexError):
                intraday = None

        liquid, reason = _liquidity(daily)
        if not liquid:
            daily = replace(
                daily,
                hard_blocks=tuple(dict.fromkeys((*daily.hard_blocks, reason))),
                risks=tuple(dict.fromkeys((*daily.risks, reason))),
                risk_score=daily.risk_score + 3,
                action="피하기 - 거래량·거래대금이 부족해 매매 후보에서 제외",
            )
        return AnalyzedStock(
            stock=stock,
            daily=daily,
            similarity=similarity,
            quote=quote,
            intraday=intraday,
            liquid=liquid,
            liquidity_reason=reason,
        )

    def _daily(self, stock: StockInfo, *, full_history: bool) -> pd.DataFrame:
        cached = self.cache.load_daily(stock, fresh_only=True)
        required_rows = 600 if full_history else 420
        if cached is not None and len(cached) >= required_rows:
            return cached.tail(650 if full_history else 460)
        frame = self.client.daily_bars(
            stock,
            calendar_days=1500 if full_history else 1000,
            max_rows=650 if full_history else 460,
        )
        # The endpoint already returns a complete recent window.  Merging an
        # obsolete cache can create a multi-year hole that looks contiguous to
        # indicator and forward-return calculations.
        self.cache.save_daily(stock, frame)
        return frame

    def _minute(self, stock: StockInfo, *, interval: int) -> pd.DataFrame:
        cached = self.cache.load_minute(stock, interval, fresh_only=True)
        if cached is not None:
            cached = regular_session_minute_bars(cached)
            if len(cached) >= 80:
                return cached
        frame = self.client.minute_bars(stock, interval_minutes=interval, max_rows=500)
        frame = regular_session_minute_bars(frame)
        if len(frame) < 80:
            raise KiwoomRestError(
                f"{stock.symbol} 정규장 {interval}분봉 데이터가 {len(frame)}개뿐이라 부족합니다."
            )
        self.cache.save_minute(stock, interval, frame)
        return frame

    @staticmethod
    def _validate_stock(stock: StockInfo, *, allow_market_proxy: bool = False) -> None:
        if allow_market_proxy:
            return
        if not is_supported_common_stock(stock):
            raise InvalidSecurityError(
                f"{stock.symbol}은(는) ETF·ETN·워런트·우선주·스팩 등 제외 대상입니다."
            )


def _liquidity(reading: IchimokuReading) -> tuple[bool, str]:
    volume = reading.avg_volume_20
    value = reading.avg_trade_value_20
    if value is None or value < MIN_AVG_DOLLAR_VOLUME:
        actual = f"${(value or 0) / 1_000_000:.1f}M"
        required = f"${MIN_AVG_DOLLAR_VOLUME / 1_000_000:.0f}M"
        return False, f"최근 20일 평균 거래대금 {actual} (기준 {required} 이상)"
    if volume is None or volume < MIN_AVG_VOLUME:
        return False, (
            f"최근 20일 평균 거래량 {(volume or 0):,.0f}주 "
            f"(기준 {MIN_AVG_VOLUME:,.0f}주 이상)"
        )
    return True, ""


def _unique_stocks(stocks: Iterator[StockInfo] | list[StockInfo]) -> list[StockInfo]:
    unique: dict[tuple[str, str], StockInfo] = {}
    for stock in stocks:
        unique[(stock.exchange, stock.symbol)] = stock
    return list(unique.values())


def _command_query(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"\s*(미국\s*)?(주식\s*)?분석\s*(해줘|해주세요|해|)?\s*$", "", cleaned, flags=re.I)
    cleaned = cleaned.strip(" `\"'")
    if not cleaned:
        raise InvalidSecurityError("종목명이나 티커를 입력해 주세요. 예: AAPL 분석해줘")
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
                "exchange": item.stock.exchange,
                "sector": item.stock.sector,
                "grade": daily.grade,
                "action": daily.action,
                "price": item.quote.price if item.quote else daily.close,
                "watch_price": daily.watch_price,
                "invalidation_price": daily.invalidation_price,
                "first_target_price": daily.first_target_price,
                "price_position": daily.price_position,
                "tk_state": daily.tk_state,
                "chikou_state": daily.chikou_state,
                "future_cloud": daily.future_cloud,
                "flat_span_b_levels": " / ".join(f"{value:.4f}" for value in daily.flat_span_b_levels),
                "volume_profile_levels": " / ".join(
                    f"{value:.4f}" for value in daily.volume_profile_levels
                ),
                "volume_ratio": daily.volume_ratio,
                "avg_volume_20": daily.avg_volume_20,
                "avg_trade_value_20_usd": daily.avg_trade_value_20,
                "liquid": item.liquid,
                "hard_blocks": " / ".join(daily.hard_blocks),
                "similar_up_count": similarity.up_count,
                "similar_sample_count": similarity.sample_count,
                "similar_up_rate": similarity.up_rate,
                "similar_median_return_pct": similarity.median_return_pct,
                "similar_match_level": similarity.match_level,
                "similar_candidate_count": similarity.candidate_count,
                "similar_average_up_return_pct": similarity.average_up_return_pct,
                "similar_average_down_return_pct": similarity.average_down_return_pct,
                "similar_invalidation_count": similarity.invalidation_count,
                "similar_invalidation_rate": similarity.invalidation_rate,
                "weekly_context": daily.higher_timeframe,
                "market_context": daily.market_context,
                "data_timestamp": daily.data_timestamp,
                "source_range": daily.source_range,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _print_help() -> None:
    print("\n사용 가능한 명령")
    print("  개별 종목 분석 : AAPL 분석해줘  (또는 애플 분석해줘)")
    print("  전체 종목 분석 : 전체 분석해줘")
    print("  명령 다시 보기 : 도움말")
    print("  프로그램 종료  : 종료")
    print("\nETF·ETN·워런트·우선주·스팩은 분석 대상에서 제외됩니다.")
    print("A+도 수익 보장이 아니라 일목 상승 구조 등급입니다.\n")


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
            raise RuntimeError("미국주식 분석기가 이미 실행 중입니다. 기존 창을 사용해 주세요.")
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


def _self_test() -> int:
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2022-01-03", periods=520)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0007, 0.012, len(dates))))
    frame = pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.002, len(dates))),
            "high": close * (1 + rng.uniform(0.002, 0.018, len(dates))),
            "low": close * (1 - rng.uniform(0.002, 0.018, len(dates))),
            "close": close,
            "volume": rng.integers(500_000, 3_000_000, len(dates)),
        },
        index=dates,
    )
    frame["trade_value"] = frame["close"] * frame["volume"]
    reading = analyze_ichimoku(frame)
    similarity = summarize_similarity(frame)
    assert reading.close > 0 and reading.cloud_top > 0
    assert 0 <= similarity.sample_count <= 30
    assert similarity.up_rate is None or 0.0 <= similarity.up_rate <= 100.0
    print("SELF_TEST_OK")
    return 0


def _credentials(store: CredentialStore, *, configure: bool) -> KiwoomCredentials:
    if configure:
        store.clear()
        return store.configure_interactively()
    loaded = store.load()
    return loaded or store.configure_interactively()


def run_command(analyzer: USStockAnalyzer, command: str) -> bool:
    normalized = re.sub(r"\s+", " ", command.strip()).casefold()
    if normalized in {"종료", "q", "quit", "exit"}:
        return False
    if normalized in {"도움말", "명령어", "?", "help"}:
        _print_help()
        return True
    if normalized in {"전체 분석해줘", "전체분석해줘", "미국 전체 분석해줘", "미국주식 전체 분석해줘"}:
        results, failures, path, _ = analyzer.analyze_all()
        interest_count = sum(
            item.liquid
            and item.daily.grade in {"A+", "A"}
            and not item.daily.hard_blocks
            and item.daily.higher_timeframe == "주봉도 상승 방향"
            and "모두 상승" in item.daily.market_context
            for item in results
        )
        print(f"\n완료: {len(results):,}개 분석 · 관심 후보 {interest_count:,}개 · 오류 {len(failures):,}개")
        print(f"HTML 보고서: {path}")
        print(f"Markdown 보고서: {path.with_suffix('.md')}\n")
        return True
    if "분석" in normalized:
        result, path = analyzer.analyze_individual(command)
        print("\n" + one_line(result))
        print(f"현재가: {usd(result.quote.price if result.quote else result.daily.close)}")
        print(
            f"확인 가격 {usd(result.daily.watch_price)} · "
            f"시나리오 무효 {usd(result.daily.invalidation_price)} · "
            f"첫 목표 후보 {usd(result.daily.first_target_price)}"
        )
        if result.daily.hard_blocks:
            print("기다리는 이유: " + " / ".join(result.daily.hard_blocks[:3]))
        print(f"HTML 보고서: {path}")
        print(f"Markdown 보고서: {path.with_suffix('.md')}\n")
        return True
    print("명령을 이해하지 못했습니다. '도움말'을 입력해 주세요.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="키움 REST 미국주식 일목균형표 분석기")
    parser.add_argument("--configure", action="store_true", help="암호화 저장된 키움 API 키 다시 설정")
    parser.add_argument("--once", default="", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test()
    try:
        credentials = _credentials(CredentialStore(CREDENTIAL_PATH), configure=args.configure)
        client = KiwoomRestClient(credentials)
        analyzer = USStockAnalyzer(client, CacheStore(CACHE_DIR))
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        with single_instance():
            print("=" * 46)
            print(" Real2 미국주식 일목균형표 분석기")
            print("=" * 46)
            print("데이터: 키움 REST API 미국주식 / 통화: USD")
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
    except (CredentialError, KiwoomRestError, InvalidSecurityError, RuntimeError) as exc:
        print(f"\n오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
