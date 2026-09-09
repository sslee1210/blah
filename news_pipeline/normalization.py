from __future__ import annotations

"""Deterministic, non-scoring normalization helpers."""

from datetime import date, datetime, time, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
import re
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


_EVENT_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("GUIDANCE", ("guidance", "outlook", "forecast", "전망", "가이던스", "실적전망")),
    ("BUYBACK", ("buyback", "share repurchase", "자사주", "자기주식취득", "자기주식처분")),
    ("DIVIDEND", ("dividend", "배당", "현금배당")),
    ("M&A", ("merger", "acquisition", "acquire", "takeover", "합병", "인수", "영업양수", "영업양도")),
    ("SUPPLY_CONTRACT", ("supply contract", "contract win", "공급계약", "수주", "단일판매", "계약체결")),
    ("LAWSUIT", ("lawsuit", "litigation", "legal proceeding", "소송", "피소", "중재")),
    ("CAPITAL_RAISE", ("capital raise", "offering", "rights issue", "유상증자", "무상증자", "전환사채", "신주인수권")),
    ("MANAGEMENT_CHANGE", ("chief executive", "ceo", "director resign", "임원", "대표이사", "경영진", "사임")),
    ("CORPORATE_ACTION", ("stock split", "reverse split", "tender offer", "분할", "주식병합", "감자", "공개매수")),
    ("EARNINGS", ("earnings", "results of operations", "quarterly report", "annual report", "실적", "잠정", "분기보고서", "반기보고서", "사업보고서")),
    ("REGULATION", ("regulation", "regulator", "antitrust", "규제", "감독당국", "공정위")),
    ("PRODUCT", ("product launch", "new product", "출시", "신제품")),
    ("TECHNOLOGY", ("technology", "patent", "semiconductor process", "기술", "특허", "공정")),
    ("CYBER", ("cyberattack", "ransomware", "data breach", "사이버", "랜섬웨어", "정보유출")),
    ("SANCTION", ("sanction", "export control", "제재", "수출통제")),
    ("TARIFF", ("tariff", "관세")),
    ("INTEREST_RATE", ("interest rate", "rate cut", "rate hike", "금리", "기준금리")),
    ("FX", ("foreign exchange", "currency", "exchange rate", "환율", "달러", "원화")),
    ("COMMODITY", ("commodity", "crude oil", "natural gas", "원자재", "유가", "천연가스")),
    ("GEOPOLITICS", ("geopolit", "war", "missile", "invasion", "지정학", "전쟁", "미사일", "침공")),
)

_GLOBAL_TERMS = ("war", "missile", "invasion", "earthquake", "flood", "geopolit", "전쟁", "미사일", "침공", "지진", "홍수")
_MACRO_TERMS = ("interest rate", "rate cut", "rate hike", "inflation", "foreign exchange", "환율", "금리", "물가", "중앙은행")
_MARKET_TERMS = ("stock market", "wall street", "kospi", "kosdaq", "nasdaq", "증시", "주식시장")

_TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}


def classify_event_type(text: str, *, form: str = "", items: str = "") -> str:
    joined = " ".join((text or "", form or "", items or "")).casefold()
    if form.upper() in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
        return "EARNINGS"
    if form.upper().startswith("8-K"):
        item_values = {item.strip() for item in re.split(r"[,; ]+", items or "") if item.strip()}
        if "2.02" in item_values:
            return "EARNINGS"
        if "2.01" in item_values:
            return "M&A"
        if "3.02" in item_values:
            return "CAPITAL_RAISE"
        if "5.02" in item_values:
            return "MANAGEMENT_CHANGE"
    for event_type, terms in _EVENT_TERMS:
        if any(term in joined for term in terms):
            return event_type
    return "OTHER"


def classify_scope(text: str, *, default: str = "COMPANY") -> str:
    """Classify evidence scope without assigning investment direction or a score."""

    joined = str(text or "").casefold()
    if any(term in joined for term in _GLOBAL_TERMS):
        return "GLOBAL_EVENT"
    if any(term in joined for term in _MACRO_TERMS):
        return "MACRO"
    if any(term in joined for term in _MARKET_TERMS):
        return "MARKET"
    return default


def source_level(source_type: str) -> int:
    normalized = str(source_type or "").upper()
    if normalized in {"OPENDART", "SEC_EDGAR"}:
        return 1
    if normalized in {"NAVER_NEWS", "GOOGLE_NEWS_RSS"}:
        return 2
    return 3


_GENERIC_COMPANY_NAMES = {"apple", "삼성", "samsung"}
_CORPORATE_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "company", "co", "ltd", "limited",
    "electronics", "motor", "motors", "전자", "주식회사", "하이닉스", "현대차", "현대자동차",
}
_LEGAL_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "company", "co", "ltd", "limited", "주식회사"}
_COMPANY_IMPACT_TERMS = {
    "earnings", "revenue", "profit", "guidance", "forecast", "shares", "stock", "company",
    "contract", "acquisition", "merger", "lawsuit", "regulator", "launch", "announces",
    "iphone", "ipad", "mac", "tim cook", "실적", "매출", "영업이익", "순이익", "주가",
    "주식", "공시", "계약", "수주", "인수", "합병", "소송", "규제", "출시", "발표",
}
_PRODUCT_ONLY_PATTERNS = {
    "MSFT": ("microsoft office", "office 365 tips", "windows keyboard shortcut"),
}
_SAMSUNG_ELECTRONICS_AFFILIATES = (
    "삼성생명", "삼성화재", "삼성물산", "삼성중공업", "삼성증권", "삼성카드",
    "삼성바이오로직스", "삼성에스디에스", "samsung life", "samsung c&t",
    "samsung heavy industries", "samsung securities", "samsung biologics",
)


def _entity_name(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", " ", str(value or "").casefold()).strip()


def _corporate_root(value: str) -> str:
    tokens = _entity_name(value).split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _contains_name(text: str, name: str) -> bool:
    haystack = f" {_entity_name(text)} "
    needle = _entity_name(name)
    return bool(needle) and f" {needle} " in haystack


def company_news_relevance(
    text: str,
    *,
    company_name: str,
    symbol: str,
    aliases: Iterable[str] = (),
    organizations: Iterable[str] = (),
) -> dict[str, object]:
    """Conservative title/entity gate for current company-news search results.

    Search-query membership and ticker-only matches are intentionally insufficient.
    The result is metadata only and is not an investment score.
    """

    normalized_symbol = str(symbol or "").upper().strip()
    normalized_text = _entity_name(text)
    names = tuple(dict.fromkeys(value for value in (company_name, *aliases) if str(value).strip()))
    roots = tuple(dict.fromkeys(_corporate_root(value) for value in names if _corporate_root(value)))

    if normalized_symbol == "005930" or "삼성전자" in {_entity_name(value) for value in names}:
        if any(_contains_name(text, affiliate) for affiliate in _SAMSUNG_ELECTRONICS_AFFILIATES) and not _contains_name(text, "삼성전자"):
            return {
                "accepted": False,
                "status": "REJECTED_GENERIC",
                "scope": classify_scope(text, default="SECTOR"),
                "matches": (),
                "reason": "different_samsung_affiliate",
            }

    impact_context = any(term in normalized_text for term in _COMPANY_IMPACT_TERMS)
    product_only = any(
        phrase in normalized_text
        for phrase in _PRODUCT_ONLY_PATTERNS.get(normalized_symbol, ())
    ) and not impact_context

    matches: list[str] = []
    for name in names:
        normalized_name = _entity_name(name)
        root = _corporate_root(name)
        if _contains_name(text, normalized_name):
            matches.append(name)
            continue
        if root and _contains_name(text, root):
            if root not in _GENERIC_COMPANY_NAMES or impact_context:
                matches.append(root)
    organization_matches = matched_aliases(organizations, (*names, *roots))
    matches.extend(organization_matches)

    if matches and not product_only:
        return {
            "accepted": True,
            "status": "DIRECT_COMPANY",
            "scope": "COMPANY",
            "matches": tuple(dict.fromkeys(matches)),
            "reason": "exact_company_name_or_verified_alias",
        }

    ticker_pattern = re.compile(rf"(?:\${re.escape(normalized_symbol)}\b|\({re.escape(normalized_symbol)}\))", re.IGNORECASE) if normalized_symbol else None
    ticker_only = bool(ticker_pattern and ticker_pattern.search(str(text or "")))
    return {
        "accepted": False,
        "status": "REJECTED_GENERIC" if product_only or ticker_only else "UNASSESSED",
        "scope": classify_scope(text, default="SECTOR"),
        "matches": tuple(dict.fromkeys(matches)),
        "reason": "product_only_context" if product_only else "ticker_only_low_trust" if ticker_only else "no_direct_company_mention",
    }


def context_relevance_metadata(
    text: str, *, target_market: str, target_sector: str = "", company_name: str = ""
) -> dict[str, str]:
    """Record coarse event relevance for later validation without changing scores."""

    joined = _entity_name(text)
    market = str(target_market or "").upper()
    us_terms = ("federal reserve", "fed ", "wall street", "nasdaq", "s p 500", "united states", "미국")
    kr_terms = ("kospi", "kosdaq", "korea", "한국", "코스피", "코스닥", "한국은행")
    supply_chain_terms = ("semiconductor", "chip", "shipping", "oil", "supply chain", "반도체", "해운", "유가", "공급망")
    distant_local_terms = ("nepal", "네팔")

    if market == "US" and any(term in joined for term in us_terms):
        market_relevance = "HIGH"
    elif market == "KR" and any(term in joined for term in kr_terms):
        market_relevance = "HIGH"
    elif any(term in joined for term in distant_local_terms) and not any(term in joined for term in supply_chain_terms):
        market_relevance = "LOW"
    elif any(term in joined for term in supply_chain_terms):
        market_relevance = "MEDIUM"
    else:
        market_relevance = "LOW"

    sector_text = _entity_name(target_sector)
    semiconductor_terms = ("semiconductor", "chip", "반도체", "electronics", "전자")
    sector_relevance = (
        "HIGH"
        if sector_text and any(term in joined for term in semiconductor_terms) and any(term in sector_text for term in semiconductor_terms)
        else "LOW" if target_sector else "UNASSESSED"
    )
    company_relevance = "HIGH" if company_name and _contains_name(text, _corporate_root(company_name)) else "LOW"
    return {
        "target_market_relevance": market_relevance,
        "target_sector_relevance": sector_relevance,
        "target_company_relevance": company_relevance,
    }


def gdelt_company_relevance(organizations: Iterable[str], *, company_name: str, symbol: str, aliases: Iterable[str]) -> dict[str, object]:
    """Conservative GKG organization gate; ticker-only and substring matches are forbidden."""

    org_map = {_entity_name(value): value for value in organizations if _entity_name(value)}
    candidates = tuple(dict.fromkeys((company_name, *aliases)))
    exact_matches: list[str] = []
    for alias in candidates:
        normalized = _entity_name(alias)
        if not normalized or normalized == _entity_name(symbol):
            continue
        legal_root_match = any(
            _corporate_root(org) == _corporate_root(normalized)
            and _corporate_root(org)
            and set(org.split()) & _LEGAL_SUFFIXES
            for org in org_map
        )
        if normalized in org_map or legal_root_match:
            exact_matches.append(alias)
    if not exact_matches:
        return {"accepted": False, "status": "REJECTED_GENERIC", "scope": "SECTOR", "matches": (), "reason": "no_exact_organization_alias"}

    qualified = []
    for alias in exact_matches:
        normalized = _entity_name(alias)
        tokens = set(normalized.split())
        if normalized not in _GENERIC_COMPANY_NAMES and (len(tokens) > 1 or normalized == _entity_name(company_name)):
            qualified.append(alias)
        elif tokens & _CORPORATE_SUFFIXES:
            qualified.append(alias)
    if not qualified:
        return {"accepted": False, "status": "REJECTED_GENERIC", "scope": "SECTOR", "matches": tuple(exact_matches), "reason": "generic_name_without_legal_qualifier"}
    return {"accepted": True, "status": "DIRECT_COMPANY", "scope": "COMPANY", "matches": tuple(dict.fromkeys(qualified)), "reason": "exact_organization_alias"}


def stable_id(*parts: object, prefix: str = "evt") -> str:
    normalized = "\x1f".join(str(part or "").strip() for part in parts)
    return f"{prefix}_{sha256(normalized.encode('utf-8')).hexdigest()[:24]}"


def canonical_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value.strip())
        query = urlencode(
            sorted((key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if key.casefold() not in _TRACKING_KEYS)
        )
        host = parsed.netloc.casefold().removeprefix("www.")
        path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
        return urlunsplit((parsed.scheme.casefold() or "https", host, path, query, ""))
    except Exception:
        return value.strip()


def parse_datetime(value: str | datetime, *, default_tz: str = "UTC", date_at_end: bool = False) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("빈 날짜/시각")
        if re.fullmatch(r"\d{14}", raw):
            parsed = datetime.strptime(raw, "%Y%m%d%H%M%S")
        elif re.fullmatch(r"\d{8}", raw):
            parsed_date = datetime.strptime(raw, "%Y%m%d").date()
            parsed = datetime.combine(parsed_date, time.max if date_at_end else time.min)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            parsed_date = date.fromisoformat(raw)
            parsed = datetime.combine(parsed_date, time.max if date_at_end else time.min)
        else:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                parsed = parsedate_to_datetime(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(default_tz))
    return parsed.astimezone(timezone.utc)


def iso_utc(value: str | datetime, *, default_tz: str = "UTC", date_at_end: bool = False) -> str:
    return parse_datetime(value, default_tz=default_tz, date_at_end=date_at_end).isoformat(timespec="seconds")


def semicolon_values(value: str, *, strip_offsets: bool = False) -> tuple[str, ...]:
    output: list[str] = []
    for raw in str(value or "").split(";"):
        item = raw.strip()
        if not item:
            continue
        if strip_offsets:
            item = item.rsplit(",", 1)[0].strip()
        if item and item not in output:
            output.append(item)
    return tuple(output)


def matched_aliases(values: Iterable[str], aliases: Iterable[str]) -> tuple[str, ...]:
    haystacks = [re.sub(r"\s+", " ", value.casefold()).strip() for value in values if value]
    matches: list[str] = []
    for alias in aliases:
        needle = re.sub(r"\s+", " ", alias.casefold()).strip()
        if len(needle) < 2:
            continue
        if any(needle == hay or (len(needle) >= 5 and needle in hay) for hay in haystacks):
            matches.append(alias)
    return tuple(dict.fromkeys(matches))
