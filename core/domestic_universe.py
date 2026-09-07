from __future__ import annotations

"""KOSPI/KOSDAQ liquid common-stock universe construction."""

from dataclasses import dataclass

from .domestic_kiwoom_rest import DomesticKiwoomRestClient, _base_symbol, is_supported_domestic_stock
from .kiwoom_rest import StockInfo, number


@dataclass(frozen=True)
class DomesticUniverseCandidate:
    stock: StockInfo
    price: float
    trade_value_rank: int
    rank_score: float


def build_domestic_scan_universe(
    client: DomesticKiwoomRestClient,
    master: list[StockInfo],
    *,
    target_size: int = 160,
    per_sector_limit: int = 24,
    min_price: float = 1_000.0,
) -> tuple[list[DomesticUniverseCandidate], dict[str, int]]:
    by_symbol = {item.symbol: item for item in master}
    merged: dict[str, DomesticUniverseCandidate] = {}
    excluded = 0

    for market_code, exchange in (("001", "KOSPI"), ("101", "KOSDAQ")):
        rows = client.trading_value_top(market_code, max_rows=max(120, target_size))
        for rank, row in enumerate(rows, start=1):
            symbol = _base_symbol(row.get("stk_cd"))
            stock = by_symbol.get(symbol)
            price = number(row.get("cur_prc"), absolute=True) or 0.0
            if stock is None or stock.exchange != exchange or price < min_price:
                excluded += 1
                continue
            if not is_supported_domestic_stock(stock):
                excluded += 1
                continue
            candidate = DomesticUniverseCandidate(
                stock=stock,
                price=price,
                trade_value_rank=rank,
                # Rank within each market is intentionally the main score; the
                # market cap/current-survivor universe is not used as a fake
                # historical return predictor.
                rank_score=max(0.0, 500.0 - rank),
            )
            existing = merged.get(symbol)
            if existing is None or candidate.rank_score > existing.rank_score:
                merged[symbol] = candidate

    ordered = sorted(merged.values(), key=lambda item: (-item.rank_score, item.stock.symbol))
    selected: list[DomesticUniverseCandidate] = []
    deferred: list[DomesticUniverseCandidate] = []
    sector_counts: dict[str, int] = {}
    market_counts = {"KOSPI": 0, "KOSDAQ": 0}
    market_limit = max(1, int(target_size * 0.65))

    for candidate in ordered:
        sector = candidate.stock.sector or "미분류"
        sector_limit = max(10, per_sector_limit // 2) if sector == "미분류" else per_sector_limit
        if sector_counts.get(sector, 0) >= sector_limit:
            deferred.append(candidate)
            continue
        if market_counts.get(candidate.stock.exchange, 0) >= market_limit:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        market_counts[candidate.stock.exchange] = market_counts.get(candidate.stock.exchange, 0) + 1
        if len(selected) >= target_size:
            break

    if len(selected) < target_size:
        already = {item.stock.symbol for item in selected}
        for candidate in deferred:
            if candidate.stock.symbol in already:
                continue
            selected.append(candidate)
            already.add(candidate.stock.symbol)
            if len(selected) >= target_size:
                break

    return selected, {
        "ranked_unique": len(merged),
        "excluded_non_common_or_low_price": excluded,
        "selected": len(selected),
        "sector_count": len({item.stock.sector for item in selected}),
        "kospi_selected": sum(item.stock.exchange == "KOSPI" for item in selected),
        "kosdaq_selected": sum(item.stock.exchange == "KOSDAQ" for item in selected),
    }
