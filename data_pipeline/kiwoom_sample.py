from __future__ import annotations

"""Kiwoom-backed *pipeline validation* sample, not a survivor-safe archive."""

from pathlib import Path

import pandas as pd

from core.credentials import CredentialStore, KiwoomCredentials
from core.domestic_kiwoom_rest import DomesticKiwoomRestClient
from core.domestic_universe import build_domestic_scan_universe
from core.kiwoom_rest import KiwoomRestClient, StockInfo
from core.storage import CacheStore
from core.universe import build_scan_universe

from .models import CollectionRequest


class KiwoomSampleProvider:
    source_name = "kiwoom_rest_sample"
    source_url = "https://openapi.kiwoom.com/"
    license_scope = "계정 승인 범위 내 개인 내부사용; 재배포 금지 여부는 계정 약관 확인 필요"
    timezone_by_market = {"US": "America/New_York", "KR": "Asia/Seoul"}
    price_adjustment = "provider_unspecified (request uses upd_stkpc_tp=1)"
    corporate_action_policy = "unknown; no separate raw/adjusted/factor series"
    survivorship_safe = False
    point_in_time_level = "current snapshot plus recent history; no historical universe"
    limitations = (
        "날짜별 적격 universe가 없어 현재 종목목록의 survivorship/selection bias가 남습니다.",
        "Kiwoom 일봉은 최대 650행이고 corporate-action 조정 명세가 연구용 PIT 수준으로 검증되지 않았습니다.",
        "이 데이터셋은 수집·저장·품질검사·평가 연결을 검증하는 표본이며 성능 주장에 사용할 수 없습니다.",
    )

    def __init__(self, credentials: KiwoomCredentials) -> None:
        self.us_client = KiwoomRestClient(credentials)
        self.kr_client = DomesticKiwoomRestClient(credentials)

    def fetch_daily(self, request: CollectionRequest) -> pd.DataFrame:
        if request.market.upper() == "US":
            stock = StockInfo(
                request.symbol,
                request.exchange,
                request.name,
                request.name,
                request.sector,
                request.kind == "index",
            )
            return self.us_client.daily_bars(stock, max_rows=650)
        if request.kind == "index":
            index_code = request.provider_symbol or request.symbol
            return self.kr_client.index_daily_bars(index_code, max_rows=600)
        stock = StockInfo(
            request.symbol,
            request.exchange,
            request.name,
            "",
            request.sector,
            False,
        )
        return self.kr_client.daily_bars(stock, max_rows=650)


def credentials_from_project(project_root: Path) -> KiwoomCredentials:
    path = Path(project_root) / ".runtime" / "kiwoom_rest_credentials.dat"
    credentials = CredentialStore(path).load()
    if credentials is None:
        raise RuntimeError("저장된 Kiwoom REST 자격증명이 없습니다. 기존 --configure 절차를 먼저 실행하십시오.")
    return credentials


def sample_requests(
    provider: KiwoomSampleProvider,
    *,
    project_root: Path,
    market: str,
    sample_size: int,
) -> tuple[list[CollectionRequest], list[dict[str, object]]]:
    requests: list[CollectionRequest] = []
    universe_rows: list[dict[str, object]] = []
    markets = ("US", "KR") if market.upper() == "BOTH" else (market.upper(),)
    if "US" in markets:
        master = CacheStore(Path(project_root) / ".us_ichimoku_cache").load_master(max_age_hours=None)
        if not master:
            master = provider.us_client.stock_master()
        candidates, _ = build_scan_universe(
            provider.us_client,
            master,
            target_size=sample_size,
            per_sector_limit=max(4, sample_size // 4),
        )
        for candidate in candidates[:sample_size]:
            stock = candidate.stock
            requests.append(_stock_request("US", stock))
        requests.extend(
            [
                CollectionRequest("US", "SPY", "NY", kind="index", name="SPDR S&P 500 ETF", security_type="market_proxy"),
                CollectionRequest("US", "QQQ", "ND", kind="index", name="Invesco QQQ", security_type="market_proxy"),
            ]
        )
    if "KR" in markets:
        master = provider.kr_client.stock_master()
        candidates, _ = build_domestic_scan_universe(
            provider.kr_client,
            master,
            target_size=sample_size,
            per_sector_limit=max(4, sample_size // 4),
        )
        for candidate in candidates[:sample_size]:
            requests.append(_stock_request("KR", candidate.stock))
        requests.extend(
            [
                CollectionRequest("KR", "KOSPI", "KOSPI", kind="index", name="KOSPI", security_type="market_index", provider_symbol="001"),
                CollectionRequest("KR", "KOSDAQ", "KOSDAQ", kind="index", name="KOSDAQ", security_type="market_index", provider_symbol="101"),
            ]
        )
    collection_date = pd.Timestamp.now(tz="Asia/Seoul").date().isoformat()
    for item in requests:
        if item.kind != "stock":
            continue
        universe_rows.append(
            {
                "date": collection_date,
                "market": item.market,
                "symbol": item.symbol,
                "eligible": True,
                "source": "kiwoom_current_ranking",
                "point_in_time_level": "single_current_snapshot_only",
            }
        )
    return requests, universe_rows


def _stock_request(market: str, stock: StockInfo) -> CollectionRequest:
    return CollectionRequest(
        market=market,
        symbol=stock.symbol,
        exchange=stock.exchange,
        kind="stock",
        name=stock.display_name,
        sector=stock.sector,
        security_type="common_stock",
    )
