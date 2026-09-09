from __future__ import annotations

"""Liquid, diversified US common-stock universe construction."""

import math
import re
from dataclasses import dataclass
from typing import Any

from .kiwoom_rest import (
    EXCHANGE_NAMES,
    KiwoomRestClient,
    StockInfo,
    normalize_kiwoom_us_symbol,
    number,
)


EXCLUDED_NAME = re.compile(
    r"\b(ETF|ETN|ETP|WARRANTS?|RIGHTS?|UNITS?|PREFERRED|PREF|ACQUISITION CORP|"
    r"ACQUISITION CO|SPAC|DEBENTURE|NOTE DUE|2X|3X|INVERSE|ULTRA(SHORT)?)\b|"
    r"상장지수|워런트|우선주|기업인수목적",
    re.IGNORECASE,
)
EXCLUDED_SYMBOL = re.compile(r"(?:\.WS|\.W|\.U|\.R|\.PR[A-Z]?|-P[A-Z]?)$", re.IGNORECASE)
MARKET_PROXIES = {"SPY", "QQQ"}


@dataclass(frozen=True)
class UniverseCandidate:
    stock: StockInfo
    price: float
    market_cap_usd: float | None
    trade_value_usd: float | None
    rank_score: float


def is_supported_common_stock(stock: StockInfo) -> bool:
    combined_name = f"{stock.korean_name} {stock.english_name}".strip()
    return (
        stock.exchange in EXCHANGE_NAMES
        and bool(stock.symbol)
        and not stock.is_etf
        and stock.symbol.upper() not in MARKET_PROXIES
        and not EXCLUDED_NAME.search(combined_name)
        and not EXCLUDED_SYMBOL.search(stock.symbol)
    )


def build_scan_universe(
    client: KiwoomRestClient,
    master: list[StockInfo],
    *,
    target_size: int = 240,
    per_sector_limit: int = 24,
) -> tuple[list[UniverseCandidate], dict[str, int]]:
    """Blend market-cap and dollar-volume ranks, then cap sector crowding."""

    master_by_key = {
        (item.exchange, normalize_kiwoom_us_symbol(item.symbol)): item for item in master
    }

    cap_rows = client.ranking("usa20550", max_rows=max(target_size * 2, 300))
    value_rows = client.ranking("usa20540", max_rows=max(target_size * 2, 300))
    merged: dict[tuple[str, str], dict[str, Any]] = {}

    def ingest(rows: list[dict[str, Any]], source: str) -> None:
        for index, row in enumerate(rows, start=1):
            symbol = normalize_kiwoom_us_symbol(str(row.get("stk_cd", "")))
            exchange = str(row.get("stex_tp", "")).strip().upper()
            if not symbol or exchange not in EXCHANGE_NAMES:
                continue
            key = (exchange, symbol)
            record = merged.setdefault(key, {"row": row, "cap_rank": None, "value_rank": None})
            record[f"{source}_rank"] = index
            if source == "cap":
                record["market_cap_usd"] = _scaled(row.get("mac"), 1000.0)
            else:
                record["trade_value_usd"] = _scaled(row.get("trde_prica"), 1000.0)
            if not record.get("row"):
                record["row"] = row

    ingest(cap_rows, "cap")
    ingest(value_rows, "value")
    candidates: list[UniverseCandidate] = []
    excluded = 0
    unverified = 0
    for (exchange, symbol), record in merged.items():
        row = record["row"]
        stock = master_by_key.get((exchange, symbol))
        if stock is None:
            # Ranking rows do not prove that an instrument is a common stock.
            # A stale/incomplete master must never turn an unknown ETF into one.
            unverified += 1
            continue
        price = number(row.get("cur_prc"), absolute=True) or 0.0
        if price < 5.0 or not is_supported_common_stock(stock):
            excluded += 1
            continue
        cap_rank = record.get("cap_rank")
        value_rank = record.get("value_rank")
        cap_points = max(0.0, 400.0 - float(cap_rank)) if cap_rank else 0.0
        value_points = max(0.0, 450.0 - float(value_rank)) if value_rank else 0.0
        both_bonus = 120.0 if cap_rank and value_rank else 0.0
        market_cap = record.get("market_cap_usd")
        trade_value = record.get("trade_value_usd")
        scale_bonus = 0.0
        if market_cap and market_cap > 0:
            scale_bonus += min(120.0, math.log10(market_cap) * 10.0)
        if trade_value and trade_value > 0:
            scale_bonus += min(100.0, math.log10(trade_value) * 9.0)
        candidates.append(
            UniverseCandidate(
                stock=stock,
                price=price,
                market_cap_usd=market_cap,
                trade_value_usd=trade_value,
                rank_score=cap_points + value_points + both_bonus + scale_bonus,
            )
        )

    ordered = sorted(candidates, key=lambda item: (-item.rank_score, item.stock.symbol))
    selected: list[UniverseCandidate] = []
    deferred: list[UniverseCandidate] = []
    sector_counts: dict[str, int] = {}
    for candidate in ordered:
        sector = candidate.stock.sector or "미분류"
        limit = max(8, per_sector_limit // 2) if sector == "미분류" else per_sector_limit
        if sector_counts.get(sector, 0) >= limit:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= target_size:
            break
    if len(selected) < target_size:
        already = {(item.stock.exchange, item.stock.symbol) for item in selected}
        for candidate in deferred:
            key = (candidate.stock.exchange, candidate.stock.symbol)
            if key in already:
                continue
            selected.append(candidate)
            already.add(key)
            if len(selected) >= target_size:
                break
    stats = {
        "ranked_unique": len(merged),
        "excluded_non_common_or_low_price": excluded,
        "excluded_unverified_master": unverified,
        "selected": len(selected),
        "sector_count": len({item.stock.sector for item in selected}),
    }
    return selected, stats


def _scaled(value: Any, multiplier: float) -> float | None:
    parsed = number(value, absolute=True)
    return parsed * multiplier if parsed is not None else None

