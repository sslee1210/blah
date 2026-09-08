from __future__ import annotations

"""Fail-soft market/news/event context for the stock analyzers."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import importlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable
from xml.etree import ElementTree

import requests

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
USER_AGENT = "Real-Stock-Analyzer/1.0"


@dataclass(frozen=True)
class MarketMetric:
    name: str
    symbol: str
    latest: float
    change_1d_pct: float
    change_5d_pct: float
    signal: str
    contribution: float
    weight: float = 1.0


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    source: str
    published_at: str = ""
    sentiment: float = 0.0
    risk: float = 0.0


@dataclass(frozen=True)
class MarketIntelligence:
    market: str
    scope: str
    score: int
    label: str
    market_score: int | None
    news_score: int | None
    event_risk: int | None
    metrics: tuple[MarketMetric, ...] = ()
    headlines: tuple[NewsItem, ...] = ()
    event_headlines: tuple[NewsItem, ...] = ()
    notes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    generated_at: str = ""

    @property
    def adverse(self) -> bool:
        return self.score < 42 or (self.event_risk is not None and self.event_risk >= 75)

    @property
    def favorable(self) -> bool:
        return self.score >= 65 and (self.event_risk is None or self.event_risk < 60)

    @property
    def one_line(self) -> str:
        parts = [f"환경 {self.score}/100 · {self.label}"]
        if self.market_score is not None:
            parts.append(f"시장 {self.market_score}")
        if self.news_score is not None:
            parts.append(f"뉴스 {self.news_score}")
        if self.event_risk is not None:
            parts.append(f"이벤트 위험 {self.event_risk}")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MarketIntelligence":
        return cls(
            market=str(payload.get("market", "")),
            scope=str(payload.get("scope", "")),
            score=int(payload.get("score", 50)),
            label=str(payload.get("label", "정보 부족")),
            market_score=_optional_int(payload.get("market_score")),
            news_score=_optional_int(payload.get("news_score")),
            event_risk=_optional_int(payload.get("event_risk")),
            metrics=tuple(MarketMetric(**item) for item in payload.get("metrics", []) if isinstance(item, dict)),
            headlines=tuple(NewsItem(**item) for item in payload.get("headlines", []) if isinstance(item, dict)),
            event_headlines=tuple(NewsItem(**item) for item in payload.get("event_headlines", []) if isinstance(item, dict)),
            notes=tuple(str(item) for item in payload.get("notes", [])),
            sources=tuple(str(item) for item in payload.get("sources", [])),
            generated_at=str(payload.get("generated_at", "")),
        )


class MarketIntelligenceService:
    def __init__(self, cache_path: Path, *, enabled: bool | None = None, session: requests.Session | None = None) -> None:
        self.cache_path = Path(cache_path)
        self.enabled = _env_bool("REAL_INTELLIGENCE_ENABLED", True) if enabled is None else enabled
        self.timeout = max(2.0, min(20.0, float(os.getenv("REAL_INTELLIGENCE_TIMEOUT", "4"))))
        self.cache_minutes = max(1, min(240, int(os.getenv("REAL_INTELLIGENCE_CACHE_MINUTES", "20"))))
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def market_overview(self, market: str) -> MarketIntelligence | None:
        market = market.upper()
        if not self.enabled or market not in {"KR", "US"}:
            return None
        key = f"market:{market}"
        cached = self._load_cache(key)
        if cached is not None:
            return cached

        notes: list[str] = []
        sources: list[str] = []
        metrics, metric_note = self._market_metrics(market)
        if metric_note:
            notes.append(metric_note)
        if metrics:
            sources.append("FinanceDataReader")
        market_score = _market_metric_score(metrics)

        if market == "KR" and self._has_naver_credentials:
            market_news, error = self._naver_search("코스피 코스닥 증시 금리 환율 반도체 수출", display=12)
            if market_news:
                sources.append("Naver News Search API")
        else:
            query = ("코스피 코스닥 증시 금리 환율 반도체 수출" if market == "KR" else
                     'United States Federal Reserve Wall Street market economy inflation rates')
            market_news, error = self._google_news_search(query, market=market, max_records=12, when="2d")
            if market_news:
                sources.append("Google News RSS")
        if error:
            notes.append(error)

        event_query = '(war OR missile OR sanction OR tariff OR cyberattack OR earthquake OR flood OR "supply disruption") (oil OR shipping OR semiconductor OR economy OR market)'
        event_items, event_error = self._gdelt_search(event_query, max_records=12, timespan="1d")
        if event_items:
            sources.append("GDELT DOC 2.0")
        if event_error:
            fallback, fallback_error = self._google_news_search(
                "war OR missile OR sanction OR tariff OR cyberattack OR earthquake OR flood",
                market="US",
                max_records=12,
                when="1d",
            )
            if fallback:
                event_items = fallback
                sources.append("Google News RSS")
                notes.append("GDELT 응답 지연으로 글로벌 이벤트는 Google News RSS 대체 소스를 사용")
                event_error = None
            else:
                notes.append(event_error)
                if fallback_error:
                    notes.append(fallback_error)

        news_score = _news_score(market_news) if market_news or error is None else None
        event_risk = _event_risk(event_items) if event_items or event_error is None else None
        score, label = _combine_environment_score(market_score, news_score, event_risk)
        result = MarketIntelligence(
            market=market,
            scope="국내 시장" if market == "KR" else "미국 시장",
            score=score,
            label=label,
            market_score=market_score,
            news_score=news_score,
            event_risk=event_risk,
            metrics=tuple(metrics),
            headlines=tuple(_dedupe_news(market_news)[:8]),
            event_headlines=tuple(_dedupe_news(event_items)[:6]),
            notes=tuple(dict.fromkeys(notes)),
            sources=tuple(dict.fromkeys(sources)),
            generated_at=_utc_now_iso(),
        )
        self._save_cache(key, result)
        return result

    def stock_overview(self, *, market: str, symbol: str, name: str, sector: str = "", base: MarketIntelligence | None = None) -> MarketIntelligence | None:
        market = market.upper()
        if not self.enabled:
            return None
        key = f"stock:{market}:{symbol.upper()}"
        cached = self._load_cache(key)
        if cached is not None:
            return cached
        base = base or self.market_overview(market)
        notes = list(base.notes if base else ())
        sources = list(base.sources if base else ())

        if market == "KR" and self._has_naver_credentials:
            query = " ".join(part for part in (name, sector) if part).strip() or symbol
            stock_news, error = self._naver_search(query, display=15)
            if stock_news:
                sources.append("Naver News Search API")
        else:
            ticker = re.sub(r"[^A-Z0-9.-]", "", symbol.upper())
            query = " ".join(part for part in (name, ticker, sector, "stock earnings company") if part).strip()
            stock_news, error = self._google_news_search(query, market=market, max_records=15, when="3d")
            if stock_news:
                sources.append("Google News RSS")
            if market == "KR" and not self._has_naver_credentials:
                notes.append("NAVER_CLIENT_ID/NAVER_CLIENT_SECRET 미설정: 국내 종목 뉴스는 Google News RSS 보조 검색 사용")
        if error:
            notes.append(error)

        stock_score = _news_score(stock_news) if stock_news else None
        base_news_score = base.news_score if base else None
        if stock_score is not None and base_news_score is not None:
            news_score = round(stock_score * 0.7 + base_news_score * 0.3)
        else:
            news_score = stock_score if stock_score is not None else base_news_score
        market_score = base.market_score if base else None
        event_risk = base.event_risk if base else None
        score, label = _combine_environment_score(market_score, news_score, event_risk)
        result = MarketIntelligence(
            market=market,
            scope=f"{name or symbol} ({symbol})",
            score=score,
            label=label,
            market_score=market_score,
            news_score=news_score,
            event_risk=event_risk,
            metrics=base.metrics if base else (),
            headlines=tuple(_dedupe_news(stock_news)[:8] or (base.headlines if base else ())),
            event_headlines=base.event_headlines if base else (),
            notes=tuple(dict.fromkeys(notes)),
            sources=tuple(dict.fromkeys(sources)),
            generated_at=_utc_now_iso(),
        )
        self._save_cache(key, result)
        return result

    @property
    def _has_naver_credentials(self) -> bool:
        return bool(os.getenv("NAVER_CLIENT_ID") and os.getenv("NAVER_CLIENT_SECRET"))

    def _market_metrics(self, market: str) -> tuple[list[MarketMetric], str | None]:
        try:
            fdr = importlib.import_module("FinanceDataReader")
        except Exception:
            return [], "FinanceDataReader 미설치: 글로벌 지수·환율 보조 점수는 생략"
        configs = _METRIC_CONFIG[market]
        start = (datetime.now(timezone.utc) - timedelta(days=18)).date().isoformat()
        results: dict[str, MarketMetric] = {}
        errors: list[str] = []

        def fetch(config: dict[str, Any]) -> MarketMetric:
            frame = fdr.DataReader(config["symbol"], start)
            close = frame["Close"].dropna().astype(float)
            if len(close) < 2:
                raise ValueError("가격 행 부족")
            latest = float(close.iloc[-1])
            one = _pct_change(latest, float(close.iloc[-2]))
            base5 = float(close.iloc[-6]) if len(close) >= 6 else float(close.iloc[0])
            five = _pct_change(latest, base5)
            contribution = float(config["direction"]) * (0.65 * _clip(one / float(config["scale1"]), -1, 1) + 0.35 * _clip(five / float(config["scale5"]), -1, 1))
            signal = "우호" if contribution >= 0.18 else "부담" if contribution <= -0.18 else "중립"
            weight = float(config["weight"])
            return MarketMetric(config["name"], config["symbol"], latest, one, five, signal, contribution * weight, weight)

        with ThreadPoolExecutor(max_workers=min(4, len(configs))) as executor:
            future_map = {executor.submit(fetch, config): config for config in configs}
            for future in as_completed(future_map):
                config = future_map[future]
                try:
                    results[config["symbol"]] = future.result()
                except Exception as exc:
                    errors.append(f"{config['name']}: {str(exc)[:80]}")
        ordered = [results[cfg["symbol"]] for cfg in configs if cfg["symbol"] in results]
        note = f"일부 시장지표 조회 실패({len(errors)}개): " + " / ".join(errors[:2]) if errors else None
        return ordered, note

    def _naver_search(self, query: str, *, display: int) -> tuple[list[NewsItem], str | None]:
        headers = {"X-Naver-Client-Id": os.getenv("NAVER_CLIENT_ID", ""), "X-Naver-Client-Secret": os.getenv("NAVER_CLIENT_SECRET", ""), "User-Agent": USER_AGENT}
        try:
            response = self.session.get(NAVER_NEWS_URL, params={"query": query, "display": max(1, min(100, display)), "sort": "date"}, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return [], f"네이버 뉴스 조회 실패: {str(exc)[:120]}"
        items: list[NewsItem] = []
        for row in payload.get("items", []):
            title = _clean_html(str(row.get("title", "")))
            if not title:
                continue
            sentiment, risk = _headline_scores(title)
            items.append(NewsItem(title, str(row.get("originallink") or row.get("link") or ""), "Naver", _normalize_pubdate(str(row.get("pubDate", ""))), sentiment, risk))
        return items, None

    def _gdelt_search(self, query: str, *, max_records: int, timespan: str) -> tuple[list[NewsItem], str | None]:
        try:
            response = self.session.get(GDELT_DOC_URL, params={"query": query, "mode": "artlist", "maxrecords": max(1, min(75, max_records)), "timespan": timespan, "sort": "datedesc", "format": "json"}, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return [], f"GDELT 뉴스 조회 실패: {str(exc)[:120]}"
        items: list[NewsItem] = []
        for row in payload.get("articles", []):
            title = _clean_html(str(row.get("title", "")))
            if not title:
                continue
            sentiment, risk = _headline_scores(title)
            items.append(NewsItem(title, str(row.get("url", "")), str(row.get("domain") or "GDELT"), str(row.get("seendate", "")), sentiment, risk))
        return items, None

    def _google_news_search(
        self,
        query: str,
        *,
        market: str,
        max_records: int,
        when: str,
    ) -> tuple[list[NewsItem], str | None]:
        is_kr = market.upper() == "KR"
        lang = "ko" if is_kr else "en"
        country = "KR" if is_kr else "US"
        try:
            response = self.session.get(
                GOOGLE_NEWS_RSS_URL,
                params={
                    "q": f"{query} when:{when}",
                    "hl": f"{lang}-{country}",
                    "gl": country,
                    "ceid": f"{country}:{lang}",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except Exception as exc:
            return [], f"Google News RSS 조회 실패: {str(exc)[:120]}"
        items: list[NewsItem] = []
        for node in root.findall(".//item")[: max(1, min(50, max_records))]:
            title = _clean_html(node.findtext("title", default=""))
            if not title:
                continue
            source_node = node.find("source")
            source = _clean_html(source_node.text or "") if source_node is not None else "Google News"
            sentiment, risk = _headline_scores(title)
            items.append(
                NewsItem(
                    title=title,
                    url=node.findtext("link", default=""),
                    source=source or "Google News",
                    published_at=_normalize_pubdate(node.findtext("pubDate", default="")),
                    sentiment=sentiment,
                    risk=risk,
                )
            )
        return items, None

    def _load_cache(self, key: str) -> MarketIntelligence | None:
        try:
            if not self.cache_path.exists():
                return None
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            item = payload.get(key)
            if not isinstance(item, dict):
                return None
            saved = datetime.fromisoformat(str(item.get("saved_at", "")))
            if saved.tzinfo is None:
                saved = saved.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - saved > timedelta(minutes=self.cache_minutes):
                return None
            data = item.get("data")
            return MarketIntelligence.from_dict(data) if isinstance(data, dict) else None
        except Exception:
            return None

    def _save_cache(self, key: str, value: MarketIntelligence) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {}
            if self.cache_path.exists():
                try:
                    loaded = json.loads(self.cache_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        payload = loaded
                except Exception:
                    payload = {}
            payload[key] = {"saved_at": _utc_now_iso(), "data": value.to_dict()}
            self.cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
_METRIC_CONFIG: dict[str, tuple[dict[str, Any], ...]] = {
    "KR": (
        {"name": "KOSPI", "symbol": "KS11", "weight": 1.35, "direction": 1, "scale1": 1.5, "scale5": 4.0},
        {"name": "KOSDAQ", "symbol": "KQ11", "weight": 1.10, "direction": 1, "scale1": 1.8, "scale5": 5.0},
        {"name": "원/달러", "symbol": "USD/KRW", "weight": 0.85, "direction": -1, "scale1": 0.8, "scale5": 2.5},
        {"name": "S&P 500", "symbol": "S&P500", "weight": 0.70, "direction": 1, "scale1": 1.5, "scale5": 4.0},
        {"name": "VIX", "symbol": "VIX", "weight": 0.75, "direction": -1, "scale1": 8.0, "scale5": 18.0},
    ),
    "US": (
        {"name": "S&P 500", "symbol": "S&P500", "weight": 1.35, "direction": 1, "scale1": 1.5, "scale5": 4.0},
        {"name": "NASDAQ", "symbol": "IXIC", "weight": 1.20, "direction": 1, "scale1": 1.8, "scale5": 5.0},
        {"name": "VIX", "symbol": "VIX", "weight": 1.00, "direction": -1, "scale1": 8.0, "scale5": 18.0},
        {"name": "미국 10년물", "symbol": "US10YT", "weight": 0.60, "direction": -1, "scale1": 2.0, "scale5": 5.0},
        {"name": "Nikkei 225", "symbol": "N225", "weight": 0.35, "direction": 1, "scale1": 1.8, "scale5": 5.0},
    ),
}

_POSITIVE_TERMS = (
    "beat", "beats", "upgrade", "upgraded", "surge", "rally", "record profit", "growth", "strong demand",
    "raises outlook", "approval", "contract win", "partnership", "buyback", "dividend increase", "recovery",
    "호실적", "상향", "급등", "반등", "수주", "계약", "승인", "성장", "회복", "증가", "흑자", "사상 최대",
)
_NEGATIVE_TERMS = (
    "miss", "misses", "downgrade", "cut outlook", "plunge", "slump", "layoff", "lawsuit", "probe", "recall",
    "default", "bankruptcy", "weak demand", "decline", "loss", "fraud", "restriction", "ban",
    "실적 부진", "하향", "급락", "감소", "적자", "소송", "조사", "리콜", "규제", "제재", "파산", "부진",
)
_RISK_TERMS = (
    "war", "missile", "attack", "sanction", "tariff", "earthquake", "flood", "wildfire", "cyberattack",
    "strike", "blockade", "supply disruption", "emergency", "conflict", "invasion", "explosion",
    "전쟁", "미사일", "공격", "제재", "관세", "지진", "홍수", "산불", "사이버 공격", "파업", "봉쇄", "공급 차질",
)
_STRONG_RISK_TERMS = ("war", "invasion", "missile", "blockade", "전쟁", "침공", "미사일", "봉쇄")


def _headline_scores(title: str) -> tuple[float, float]:
    text = title.casefold()
    positive = sum(term in text for term in _POSITIVE_TERMS)
    negative = sum(term in text for term in _NEGATIVE_TERMS)
    sentiment = _clip((positive - negative) / max(1.0, positive + negative), -1.0, 1.0)
    risk_hits = sum(term in text for term in _RISK_TERMS)
    strong_hits = sum(term in text for term in _STRONG_RISK_TERMS)
    risk = _clip(0.28 * risk_hits + 0.35 * strong_hits, 0.0, 1.0)
    return sentiment, risk


def _market_metric_score(metrics: Iterable[MarketMetric]) -> int | None:
    values = list(metrics)
    if not values:
        return None
    weighted = sum(item.contribution for item in values) / max(1.0, sum(item.weight for item in values))
    return int(round(_clip(50.0 + weighted * 34.0, 0.0, 100.0)))


def _news_score(items: Iterable[NewsItem]) -> int:
    values = list(items)
    if not values:
        return 50
    mean = sum(item.sentiment for item in values) / len(values)
    return int(round(_clip(50.0 + mean * 32.0, 0.0, 100.0)))


def _event_risk(items: Iterable[NewsItem]) -> int:
    values = list(items)
    if not values:
        return 15
    risks = [item.risk for item in values]
    mean = sum(risks) / len(risks)
    peak = max(risks)
    volume = min(1.0, math.log1p(len(values)) / math.log(13.0))
    return int(round(_clip(10.0 + 45.0 * mean + 30.0 * peak + 10.0 * volume, 0.0, 100.0)))


def _combine_environment_score(market_score: int | None, news_score: int | None, event_risk: int | None) -> tuple[int, str]:
    pieces: list[tuple[float, float]] = []
    if market_score is not None:
        pieces.append((float(market_score), 0.55))
    if news_score is not None:
        pieces.append((float(news_score), 0.25))
    if event_risk is not None:
        pieces.append((100.0 - float(event_risk), 0.20))
    if not pieces:
        return 50, "정보 부족"
    total = sum(weight for _, weight in pieces)
    score = int(round(sum(value * weight for value, weight in pieces) / total))
    if score >= 72:
        label = "매우 우호적"
    elif score >= 60:
        label = "우호적"
    elif score >= 45:
        label = "중립"
    elif score >= 35:
        label = "부담"
    else:
        label = "매우 부담"
    return score, label


def _dedupe_news(items: Iterable[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    output: list[NewsItem] = []
    for item in items:
        key = re.sub(r"\W+", "", item.title.casefold())[:120]
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _gdelt_phrase(text: str) -> str:
    cleaned = re.sub(r"[\"()]+", " ", text or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return f'"{cleaned}"' if cleaned else ""


def _clean_html(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", text))).strip()


def _normalize_pubdate(text: str) -> str:
    try:
        value = parsedate_to_datetime(text)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat(timespec="minutes")
    except Exception:
        return text


def _pct_change(latest: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return (latest / previous - 1.0) * 100.0


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() not in {"0", "false", "no", "off"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
