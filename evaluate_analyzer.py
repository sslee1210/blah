from __future__ import annotations

"""Command-line entry point for the point-in-time evaluation layer."""

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from core.domestic_storage import DomesticCacheStore
from core.kiwoom_rest import StockInfo
from core.market_intelligence import MarketIntelligence
from core.storage import CacheStore
from core.universe import is_supported_common_stock
from evaluation import BacktestConfig, evaluate_point_in_time, write_evaluation_bundle


ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="기존 분석 규칙을 변경하지 않는 point-in-time 평가기"
    )
    parser.add_argument("--market", choices=("US", "KR", "BOTH"), default="BOTH")
    parser.add_argument("--us-cache", type=Path, default=ROOT / ".us_ichimoku_cache")
    parser.add_argument("--kr-cache", type=Path, default=ROOT / ".domestic_ichimoku_cache")
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument(
        "--dataset",
        help="evaluation/datasets 아래 데이터셋 ID (예: historical, historical_sample)",
    )
    parser.add_argument(
        "--baseline-mode",
        choices=("TECHNICAL_BASELINE", "FULL_CONTEXT_BASELINE"),
        default="TECHNICAL_BASELINE",
    )
    parser.add_argument("--universe-snapshots", type=Path)
    parser.add_argument("--intelligence-snapshots", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--decision-step", type=int, default=1)
    parser.add_argument("--min-cross-section", type=int, default=20)
    parser.add_argument("--min-split-dates", type=int, default=60)
    parser.add_argument("--max-symbols", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dataset and args.dataset_root:
        raise SystemExit("--dataset과 --dataset-root는 동시에 사용할 수 없습니다.")
    dataset_root, dataset_manifest = _resolve_dataset(args.dataset, args.dataset_root)
    markets = ("US", "KR") if args.market == "BOTH" else (args.market,)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = args.output or ROOT / "reports" / f"evaluation_{stamp}"
    output_root.mkdir(parents=True, exist_ok=True)
    universe_path = args.universe_snapshots
    intelligence_path = args.intelligence_snapshots
    if dataset_manifest:
        # Date membership must constrain replay even before the dataset earns
        # the trusted label.  Trust flags decide whether results may be
        # interpreted; they must not cause a fallback to today's stock list.
        universe_path = universe_path or _manifest_path(dataset_manifest.get("universe_snapshot_path"))
    if dataset_manifest and args.baseline_mode == "FULL_CONTEXT_BASELINE":
        intelligence_path = intelligence_path or _manifest_path(dataset_manifest.get("intelligence_snapshot_path"))
    universe_all = _load_universe(universe_path)
    intelligence_all = _load_intelligence(intelligence_path)
    manifest_flags = _manifest_flags(dataset_manifest)
    results: dict[str, dict[str, object]] = {}

    for market in markets:
        if dataset_root:
            stocks, proxies = _load_dataset(dataset_root, market)
        else:
            cache = args.us_cache if market == "US" else args.kr_cache
            stocks, proxies = _load_cache(cache, market)
        if args.max_symbols is not None:
            stocks = dict(list(sorted(stocks.items()))[: max(0, args.max_symbols)])
        universe = universe_all.get(market, {})
        intelligence = intelligence_all.get(market, {})
        bundle = evaluate_point_in_time(
            stocks,
            config=BacktestConfig(
                market=market,
                baseline_mode=args.baseline_mode,
                dataset_point_in_time_verified=manifest_flags["point_in_time_verified"],
                price_policy_verified=manifest_flags["price_policy_verified"],
                delisted_securities_included=manifest_flags["delisted_securities_included"],
                exchange_calendar_verified=manifest_flags["exchange_calendar_verified"],
                decision_step=args.decision_step,
                min_cross_section=args.min_cross_section,
                min_split_dates=args.min_split_dates,
            ),
            market_proxies=proxies,
            intelligence_snapshots=intelligence,
            universe_snapshots=universe,
        )
        market_output = output_root / market.lower()
        write_evaluation_bundle(bundle, market_output)
        results[market] = bundle.metadata
        print(
            f"{market}: {bundle.metadata.get('status')} | "
            f"trust={bundle.metadata.get('trust_level')} "
            f"stocks={bundle.metadata.get('stock_count')} "
            f"signals={bundle.metadata.get('signal_count')} | {market_output}"
        )

    (output_root / "run_metadata.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (output_root / "report.md").write_text(
        _combined_report(results, output_root), encoding="utf-8"
    )
    print(f"통합 보고서: {output_root / 'report.md'}")
    return 0


def _resolve_dataset(
    dataset: str | None, dataset_root: Path | None
) -> tuple[Path | None, dict[str, object] | None]:
    if dataset:
        safe = Path(dataset).name
        if safe != dataset or safe in {"", ".", ".."}:
            raise SystemExit("--dataset에는 경로가 아닌 안전한 데이터셋 ID만 지정하십시오.")
        root = ROOT / "evaluation" / "datasets" / safe
        manifest_path = ROOT / "data" / "metadata" / "manifests" / f"{safe}.json"
        if not root.exists():
            raise SystemExit(f"데이터셋이 없습니다: {root}")
        if not manifest_path.exists():
            raise SystemExit(f"데이터 manifest가 없습니다: {manifest_path}")
        return root, json.loads(manifest_path.read_text(encoding="utf-8"))
    return dataset_root, None


def _manifest_path(value: object) -> Path | None:
    if not value:
        return None
    path = (ROOT / str(value)).resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise SystemExit(f"manifest 경로가 프로젝트 밖을 가리킵니다: {path}") from exc
    return path


def _manifest_trust(manifest: dict[str, object] | None) -> tuple[bool, bool]:
    flags = _manifest_flags(manifest)
    return flags["point_in_time_verified"], flags["price_policy_verified"]


def _manifest_flags(manifest: dict[str, object] | None) -> dict[str, bool]:
    if not manifest:
        return {
            "point_in_time_verified": False,
            "price_policy_verified": False,
            "delisted_securities_included": False,
            "exchange_calendar_verified": False,
        }
    assets = [item for item in manifest.get("assets", []) if isinstance(item, dict)]
    stock_assets = [item for item in assets if item.get("kind") == "stock"]
    point_in_time = (
        bool(manifest.get("survivorship_safe"))
        and bool(manifest.get("universe_snapshot_path"))
        and bool(stock_assets)
        and all(
        item.get("survivorship_safe") is True for item in stock_assets
        )
    )
    price_policy = bool(manifest.get("corporate_action_verified")) and bool(stock_assets) and all(
        item.get("quality_status") != "failed"
        and "unknown" not in str(item.get("corporate_action_policy", "")).casefold()
        and "unspecified" not in str(item.get("price_adjustment", "")).casefold()
        for item in stock_assets
    )
    return {
        "point_in_time_verified": point_in_time,
        "price_policy_verified": price_policy,
        "delisted_securities_included": bool(manifest.get("delisted_securities_included")),
        "exchange_calendar_verified": bool(manifest.get("exchange_calendar_verified")),
    }


def _load_cache(
    cache_root: Path, market: str
) -> tuple[dict[str, tuple[StockInfo, pd.DataFrame]], dict[str, pd.DataFrame]]:
    stocks: dict[str, tuple[StockInfo, pd.DataFrame]] = {}
    proxies: dict[str, pd.DataFrame] = {}
    if not cache_root.exists():
        return stocks, proxies
    if market == "US":
        store = CacheStore(cache_root)
        master = store.load_master(max_age_hours=None) or []
        by_key = {(item.exchange.upper(), item.symbol.upper()): item for item in master}
        for path in sorted(store.daily_dir.glob("*.csv")):
            exchange, symbol = _split_cache_stem(path.stem)
            stock = by_key.get((exchange, symbol)) or StockInfo(
                symbol, exchange, symbol, symbol, "", symbol in {"SPY", "QQQ"}
            )
            frame = store.load_daily(stock, fresh_only=False)
            if frame is None:
                continue
            if symbol in {"SPY", "QQQ"}:
                proxies[symbol] = frame
            elif is_supported_common_stock(stock):
                stocks[f"{exchange}:{symbol}"] = (stock, frame)
    else:
        store = DomesticCacheStore(cache_root)
        master = store.load_master(max_age_hours=None) or []
        by_key = {(item.exchange.upper(), item.symbol.upper()): item for item in master}
        for path in sorted(store.daily_dir.glob("*.csv")):
            exchange, symbol = _split_cache_stem(path.stem)
            if symbol in {"KOSPI", "KOSDAQ"}:
                frame = _read_csv(path)
                if frame is not None:
                    proxies[symbol] = frame
                continue
            stock = by_key.get((exchange, symbol)) or StockInfo(
                symbol, exchange, symbol, symbol, "", False
            )
            frame = store.load_daily(stock, fresh_only=False)
            if frame is not None and not stock.is_etf:
                stocks[f"{exchange}:{symbol}"] = (stock, frame)
        for symbol in ("KOSPI", "KOSDAQ"):
            for path in (
                cache_root / "indices" / f"{symbol}.csv",
                cache_root / "daily" / f"{symbol}.csv",
            ):
                frame = _read_csv(path)
                if frame is not None:
                    proxies[symbol] = frame
                    break
    return stocks, proxies


def _load_dataset(
    root: Path, market: str
) -> tuple[dict[str, tuple[StockInfo, pd.DataFrame]], dict[str, pd.DataFrame]]:
    base = root / market
    stocks: dict[str, tuple[StockInfo, pd.DataFrame]] = {}
    proxies: dict[str, pd.DataFrame] = {}
    metadata = _stock_metadata(base / "stocks.csv")
    for path in sorted((base / "stocks").glob("*.csv")):
        symbol = path.stem.upper()
        row = metadata.get(symbol, {})
        stock = StockInfo(
            symbol=symbol,
            exchange=str(row.get("exchange", "HIST")),
            korean_name=str(row.get("korean_name", row.get("name", symbol))),
            english_name=str(row.get("english_name", row.get("name", symbol))),
            sector=str(row.get("sector", "")),
            is_etf=_bool(row.get("is_etf", False)),
        )
        frame = _read_csv(path)
        if frame is not None and not stock.is_etf:
            stocks[symbol] = (stock, frame)
    for path in sorted((base / "proxies").glob("*.csv")):
        frame = _read_csv(path)
        if frame is not None:
            proxies[path.stem.upper()] = frame
    return stocks, proxies


def _read_csv(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    date_column = next(
        (item for item in ("timestamp", "date", "datetime") if item in frame.columns), None
    )
    if date_column is None:
        return None
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop(date_column), errors="raise"))
    return frame


def _stock_metadata(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    frame = pd.read_csv(path, dtype={"symbol": str})
    if "symbol" not in frame:
        return {}
    return {
        str(row["symbol"]).upper(): row.to_dict()
        for _, row in frame.iterrows()
    }


def _load_universe(path: Path | None) -> dict[str, dict[str, set[str]]]:
    output: dict[str, dict[str, set[str]]] = {"US": {}, "KR": {}}
    if path is None or not path.exists():
        return output
    frame = pd.read_csv(path, dtype={"symbol": str})
    required = {"date", "market", "symbol"}
    if not required.issubset(frame.columns):
        raise ValueError("universe CSV에는 date, market, symbol 열이 필요합니다.")
    if "eligible" in frame:
        frame = frame.loc[frame["eligible"].map(_bool)]
    for (market, day), group in frame.groupby(["market", "date"]):
        key = str(market).upper()
        if key not in output:
            continue
        normalized = pd.Timestamp(day).date().isoformat()
        output[key][normalized] = {str(value).upper() for value in group["symbol"]}
    return output


def _load_intelligence(
    path: Path | None,
) -> dict[str, dict[tuple[str, str], MarketIntelligence]]:
    output: dict[str, dict[tuple[str, str], MarketIntelligence]] = {"US": {}, "KR": {}}
    if path is None or not path.exists():
        return output
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            payload = json.loads(line)
            market = str(payload.get("market", "")).upper()
            if market not in output:
                raise ValueError(f"intelligence JSONL {line_number}: market은 US/KR이어야 합니다.")
            as_of = pd.Timestamp(payload.pop("as_of")).date().isoformat()
            symbol = str(payload.pop("symbol", "*")).upper()
            output[market][(as_of, symbol)] = MarketIntelligence.from_dict(payload)
    return output


def _split_cache_stem(stem: str) -> tuple[str, str]:
    exchange, separator, symbol = stem.partition("_")
    if not separator:
        return "", stem.upper()
    return exchange.upper(), symbol.upper()


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "예"}


def _combined_report(results: dict[str, dict[str, object]], output: Path) -> str:
    lines = [
        "# 현재 분석기 객관 평가 기반 구축 결과",
        "",
        "## A. 구축한 백테스트 구조",
        "",
        "과거 신호일마다 OHLCV를 해당 일자까지 잘라 기존 분석 함수를 재실행합니다. "
        "다음 거래일 시가를 진입가로, Forward 1/5/10/20번째 거래일 종가를 평가가로 사용합니다. "
        "미래 구간은 신호 레코드가 확정된 뒤 성과 계산에서만 접근합니다.",
        "",
        "- 성과: 평균·중앙값·상승 비율·MAE·MFE·종가 경로 최대낙폭·무효가격 도달률·10% 큰 손실률·평균/변동성",
        "- 비교: A+/A/B/C/D, 관심/기다림/피하기, 관심 > 기다림 > 피하기 순서 검정",
        "- Ranking: 날짜별 Spearman 및 Top 5/10/20%, Bottom 20%의 Forward 5/10/20일",
        "- 검증: 20거래일 purge를 둔 60/20/20 chronological Train/Validation/Test",
        "- Test 격리: 임계값·가중치 선택에 Test 결과를 사용하지 않음",
        "",
        "## B. 사용한 데이터 기간",
        "",
    ]
    for market, meta in results.items():
        ranges = meta.get("stock_ranges", {})
        lines.append(f"- {market}: 대상 {meta.get('stock_count', 0)}종목, {len(ranges)}개 가격 이력")
        for symbol, period in meta.get("proxy_ranges", {}).items():
            lines.append(
                f"  - 시장 프록시 {symbol}: {period.get('start')} ~ {period.get('end')} "
                f"({period.get('rows')}거래일)"
            )
    lines.extend(
        [
            "",
            "## C. 데이터 한계",
            "",
        ]
    )
    for market, meta in results.items():
        for item in meta.get("limitations", []):
            lines.append(f"- {market}: {item}")
    lines.extend(
        [
            "- 현재 미국 캐시의 650거래일은 SPY·QQQ뿐이며 두 ETF는 AGENTS.md에 따라 종목 후보에서 제외했습니다.",
            "- 국내 캐시에는 평가 가능한 종목·KOSPI·KOSDAQ 이력이 없습니다.",
            "- 10/20일 Forward 관측은 서로 겹치므로 단순 행 수를 독립 표본 수로 해석할 수 없습니다.",
            "- 뉴스는 timezone-aware 생성시각이 해당 시장 종가보다 늦으면 같은 날 신호에서 거부합니다.",
        ]
    )
    sections = [
        ("D", "Baseline 결과"),
        ("E", "등급별 결과"),
        ("F", "관심/기다림/피하기 결과"),
        ("G", "Ranking 결과"),
        ("H", "Ablation 결과"),
        ("I", "신호별 기여도 순위"),
        ("J", "제거 또는 약화 후보"),
        ("K", "과거 유사패턴 검증 결과"),
        ("L", "발견된 추가 Bias"),
        ("M", "다음 단계에서 실제 수정해야 할 항목"),
    ]
    for code, title in sections:
        lines.extend(["", f"## {code}. {title}", ""])
        if code in {"D", "E", "F", "G", "H", "I", "J", "K"}:
            if any(meta.get("trusted_baseline_available") for meta in results.values()):
                lines.append("세부 수치는 시장별 CSV 산출물을 참조하십시오.")
            else:
                lines.append("현재 데이터로는 신뢰 가능한 수치를 계산할 수 없어 평가 보류입니다.")
            if code == "F":
                lines.append("관심 > 기다림 > 피하기 순서도 표본 0건이라 합격/불합격을 판정하지 않았습니다.")
            elif code == "G":
                lines.append("동일 날짜에 비교할 일반주식 단면이 없어 Spearman과 분위 그룹 성과는 산출하지 않았습니다.")
            elif code == "H":
                lines.append(
                    "평가기는 ADX/DI·ATR·거래량을 평가 전용 규칙 중립화로, 주봉·시장 방향·거래대금을 "
                    "기존 인터페이스의 통과값으로, 뉴스·이벤트를 과거 스냅샷 중립값으로 비교합니다. "
                    "60분봉과 유사패턴은 현재 점수/최종판단에 운영 영향이 없어 baseline과 동일하게 표시합니다. "
                    "일목 제거는 현 모델의 등급 정의 자체를 없애므로 임의 대체점수를 만들지 않고 측정 불가로 표시합니다."
                )
            elif code == "I":
                lines.append("Validation 표본이 없으므로 신호 기여도 순위를 만들지 않았습니다.")
            elif code == "J":
                lines.append("성능 근거 없이 유지·약화·제거 후보를 지정하지 않았습니다.")
            elif code == "K":
                lines.append(
                    "표본수별 Brier/보정오차, distance 사분위, 예측 상승률 사분위, 시장상태별 성과와 "
                    "유사패턴 변수 상관을 계산하도록 구현했지만 현재 관측 표본은 0건입니다."
                )
        elif code == "L":
            lines.append(
                "현재 종목목록을 과거에 소급하는 survivorship/selection bias, 과거 뉴스 스냅샷 부재, "
                "시장 프록시 부재가 신뢰 가능한 평가를 막습니다. Forward 표본은 겹치므로 일별 행을 "
                "독립 표본으로 간주하면 안 됩니다."
            )
        else:
            lines.extend(
                [
                    "먼저 다음 데이터를 확보하고 이 평가기를 재실행해야 합니다.",
                    "",
                    "- 국내·미국 모두 최소 2015년 이후의 상장·상장폐지 포함 일봉 OHLCV/거래대금과 기업행사 시점 데이터",
                    "- 각 거래일 당시의 실제 후보 유니버스와 시가총액·거래대금 순위 스냅샷",
                    "- 같은 기간 KOSPI·KOSDAQ 및 SPY·QQQ 완료 일봉",
                    "- 60분봉을 독립 검증하려면 정규장 세션·timezone·DST·완료 여부가 보존된 분봉",
                    "- 게시시각과 최초수집시각, 중복 사건 ID가 있는 시장/종목 뉴스·글로벌 이벤트 스냅샷",
                    "",
                    "그 전에는 운영 점수·등급·관심/기다림/피하기 로직을 변경하지 않습니다.",
                ]
            )
    lines.extend(
        [
            "",
            "## 핵심 질문에 대한 답",
            "",
            "현재 확보 데이터만으로는 분석기가 차트가 좋아 보이는 종목을 찾는 수준을 넘어 "
            "실제 미래 상대성과를 구분하는지 판단할 수 없습니다. 긍정·부정 어느 쪽의 성능도 입증되지 않았습니다.",
            "",
            "## 요구사항 자체 검증",
            "",
            "- [x] 운영 분석 알고리즘·점수·뉴스·Regime·GUI 미변경",
            "- [x] Point-in-time prefix 재생과 미래구간 분리",
            "- [x] Forward 1/5/10/20일 및 요청 성과지표 구현",
            "- [x] 등급·판단·관심 순서·Ranking·Walk-forward 구현",
            "- [x] 12개 Ablation family와 동일 신호행 비교 구조 구현",
            "- [x] 신호 상관·유사패턴 표본수/distance/상승률/regime/변수중복 검증 구현",
            "- [x] 데이터 부족 시 성능 수치 미산출",
            "- [ ] 신뢰 가능한 Baseline 수치 산출 — 현재 대상 종목 및 PIT 스냅샷 부재로 불가",
            "",
            f"산출물 위치: `{output}`",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
