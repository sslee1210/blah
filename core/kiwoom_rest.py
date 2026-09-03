from __future__ import annotations

"""Small, rate-limited Kiwoom REST client for US stock market data."""

import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .credentials import KiwoomCredentials


API_BASE_URL = "https://api.kiwoom.com"
US_EASTERN = ZoneInfo("America/New_York")
KOREA = ZoneInfo("Asia/Seoul")
EXCHANGE_NAMES = {"ND": "NASDAQ", "NY": "NYSE", "NA": "AMEX"}
EXCHANGE_RANK_CODES = {"NY": "1", "ND": "2", "NA": "3"}
REGULAR_SESSION_CLOSE_MINUTE = 16 * 60
MAX_DAILY_SESSION_LAG_DAYS = 4
MAX_MINUTE_DATA_AGE_DAYS = 7


class KiwoomRestError(RuntimeError):
    """An authenticated Kiwoom REST request could not be completed."""


class InvalidSecurityError(KiwoomRestError):
    """The requested code is not a supported US common stock."""


@dataclass(frozen=True)
class StockInfo:
    symbol: str
    exchange: str
    korean_name: str
    english_name: str
    sector: str
    is_etf: bool = False

    @property
    def display_name(self) -> str:
        return self.korean_name or self.english_name or self.symbol


@dataclass(frozen=True)
class Quote:
    symbol: str
    exchange: str
    name: str
    price: float
    open: float | None
    high: float | None
    low: float | None
    volume: float | None
    prev_close: float | None
    market_cap_usd: float | None
    timestamp: datetime
    currency: str = "USD"


def number(value: Any, *, absolute: bool = False) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        result = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return abs(result) if absolute else result


def _return_code(payload: dict[str, Any]) -> int:
    try:
        return int(payload.get("return_code", 0))
    except (TypeError, ValueError):
        return -1


class _RateLimiter:
    """Process-wide spacing guard kept below Kiwoom's query ceiling."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        now_kr = datetime.now(KOREA)
        interval = 0.36 if now_kr.hour == 9 else 0.23
        with self._lock:
            wait_for = interval - (time.monotonic() - self._last)
            if wait_for > 0:
                time.sleep(wait_for)
            self._last = time.monotonic()


class KiwoomRestClient:
    """Authenticated client implementing token refresh, paging and retries."""

    def __init__(
        self,
        credentials: KiwoomCredentials,
        *,
        base_url: str = API_BASE_URL,
        timeout: float = 20.0,
        max_attempts: int = 8,
    ) -> None:
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max(2, max_attempts)
        self._token: str | None = None
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._token_lock = threading.Lock()
        self._thread_local = threading.local()
        self._limiter = _RateLimiter()

    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": "Real2-US-Ichimoku/1.0"})
            self._thread_local.session = session
        return session

    def _issue_token(self, *, force: bool = False) -> str:
        with self._token_lock:
            now = datetime.now(timezone.utc)
            if (
                not force
                and self._token
                and now + timedelta(minutes=5) < self._token_expires_at
            ):
                return self._token
            try:
                response = self._session().post(
                    f"{self.base_url}/oauth2/token",
                    json={
                        "grant_type": "client_credentials",
                        "appkey": self.credentials.appkey,
                        "secretkey": self.credentials.secretkey,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise KiwoomRestError("키움 REST 접근토큰 발급에 실패했습니다.") from exc
            token = str(payload.get("token", "")).strip()
            if not token:
                message = str(payload.get("return_msg", "인증 정보 확인 필요"))
                raise KiwoomRestError(f"키움 REST 인증 실패: {message}")
            expires = str(payload.get("expires_dt", "")).strip()
            parsed_expiry: datetime | None = None
            for fmt in ("%Y%m%d%H%M%S", "%Y%m%d"):
                try:
                    parsed_expiry = datetime.strptime(expires, fmt).replace(tzinfo=KOREA).astimezone(
                        timezone.utc
                    )
                    break
                except ValueError:
                    continue
            self._token = token
            self._token_expires_at = parsed_expiry or (now + timedelta(hours=20))
            return token

    def _request_once(
        self,
        api_id: str,
        path: str,
        body: dict[str, Any],
        *,
        cont_yn: str = "N",
        next_key: str = "",
        force_token: bool = False,
    ) -> tuple[dict[str, Any], requests.structures.CaseInsensitiveDict[str]]:
        token = self._issue_token(force=force_token)
        headers = {
            "authorization": f"Bearer {token}",
            "api-id": api_id,
            "cont-yn": cont_yn,
            "next-key": next_key,
            "Content-Type": "application/json;charset=UTF-8",
        }
        self._limiter.wait()
        response = self._session().post(
            f"{self.base_url}{path}",
            headers=headers,
            json=body,
            timeout=self.timeout,
        )
        if response.status_code in (401, 403):
            raise PermissionError("token")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise KiwoomRestError(f"{api_id} 응답 형식이 올바르지 않습니다.")
        if _return_code(payload) != 0:
            message = str(payload.get("return_msg", "알 수 없는 API 오류")).strip()
            lowered = message.lower()
            if "종목" in message and any(word in message for word in ("없", "오류", "확인")):
                raise InvalidSecurityError(message)
            if any(word in lowered for word in ("token", "토큰", "인증")):
                raise PermissionError("token")
            raise KiwoomRestError(f"{api_id}: {message}")
        return payload, response.headers

    def request(
        self,
        api_id: str,
        path: str,
        body: dict[str, Any],
        *,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> tuple[dict[str, Any], requests.structures.CaseInsensitiveDict[str]]:
        last_error: Exception | None = None
        token_refreshed = False
        for attempt in range(self.max_attempts):
            try:
                return self._request_once(
                    api_id,
                    path,
                    body,
                    cont_yn=cont_yn,
                    next_key=next_key,
                    force_token=False,
                )
            except InvalidSecurityError:
                raise
            except PermissionError as exc:
                last_error = exc
                if token_refreshed:
                    break
                self._issue_token(force=True)
                token_refreshed = True
                continue
            except (requests.RequestException, KiwoomRestError, ValueError) as exc:
                last_error = exc
                if attempt + 1 >= self.max_attempts:
                    break
                time.sleep(min(15.0, 0.8 * (2**attempt)))
        raise KiwoomRestError(
            f"{api_id} 요청을 {self.max_attempts}회 재시도했지만 완료하지 못했습니다: {last_error}"
        ) from last_error

    def paged(
        self,
        api_id: str,
        path: str,
        body: dict[str, Any],
        *,
        list_key: str,
        max_pages: int = 10,
        max_rows: int | None = None,
        page_delay: float = 0.0,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cont_yn = "N"
        next_key = ""
        seen_keys: set[str] = set()
        for page in range(max(1, max_pages)):
            payload, headers = self.request(
                api_id,
                path,
                body,
                cont_yn=cont_yn,
                next_key=next_key,
            )
            value = payload.get(list_key, [])
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
            if max_rows is not None and len(rows) >= max_rows:
                return rows[:max_rows]
            has_more = str(headers.get("cont-yn", "N")).upper() == "Y"
            new_key = str(headers.get("next-key", "")).strip()
            if not has_more or not new_key or new_key in seen_keys:
                break
            seen_keys.add(new_key)
            cont_yn, next_key = "Y", new_key
            if page_delay > 0:
                time.sleep(page_delay)
        return rows

    def stock_master(self, exchange: str = "%", *, max_pages: int = 8) -> list[StockInfo]:
        rows = self.paged(
            "usa10099",
            "/api/us/stkinfo",
            {"stex_tp": exchange},
            list_key="list",
            max_pages=max_pages,
            page_delay=12.1,
        )
        result: dict[tuple[str, str], StockInfo] = {}
        for row in rows:
            symbol = str(row.get("stk_cd", "")).strip().upper()
            stex = str(row.get("stex_tp", "")).strip().upper()
            if not symbol or stex not in EXCHANGE_NAMES:
                continue
            result[(stex, symbol)] = StockInfo(
                symbol=symbol,
                exchange=stex,
                korean_name=str(row.get("stk_nm", "")).strip(),
                english_name=str(row.get("stk_enm", "")).strip(),
                sector=str(row.get("upgb", "")).strip() or "미분류",
                is_etf=str(row.get("isEtf", "N")).strip().upper() == "Y",
            )
        return list(result.values())

    def stock_info(self, symbol: str, exchange: str) -> StockInfo:
        exchange = exchange.strip().upper()
        if exchange not in EXCHANGE_NAMES:
            raise InvalidSecurityError(
                f"{symbol} 종목 조회에는 ND·NY·NA 중 하나의 거래소 코드가 필요합니다."
            )
        payload, _ = self.request(
            "usa10100",
            "/api/us/stkinfo",
            {"stex_tp": exchange, "stk_cd": symbol.upper()},
        )
        stex = str(payload.get("stex_tp", exchange)).strip().upper()
        code = str(payload.get("stk_cd", symbol)).strip().upper()
        if not code or stex not in EXCHANGE_NAMES:
            raise InvalidSecurityError(f"{symbol}의 미국 거래소를 확인하지 못했습니다.")
        return StockInfo(
            symbol=code,
            exchange=stex,
            korean_name=str(payload.get("stk_nm", "")).strip(),
            english_name=str(payload.get("stk_enm", "")).strip(),
            sector=str(payload.get("upgb", "")).strip() or "미분류",
            is_etf=str(payload.get("isEtf", "N")).strip().upper() == "Y",
        )

    def quote(self, stock: StockInfo) -> Quote:
        payload, _ = self.request(
            "usa20100",
            "/api/us/mrkcond",
            {"stex_tp": stock.exchange, "stk_cd": stock.symbol},
        )
        price = number(payload.get("cur_prc"), absolute=True)
        if price is None or price <= 0:
            raise KiwoomRestError(f"{stock.symbol} 현재가가 비어 있습니다.")
        market_cap_thousand = number(payload.get("mac"), absolute=True)
        return Quote(
            symbol=stock.symbol,
            exchange=stock.exchange,
            name=str(payload.get("stk_nm", "")).strip() or stock.display_name,
            price=price,
            open=number(payload.get("open_pric"), absolute=True),
            high=number(payload.get("high_pric"), absolute=True),
            low=number(payload.get("low_pric"), absolute=True),
            volume=number(payload.get("acc_trde_qty"), absolute=True),
            prev_close=number(payload.get("base_close_pric"), absolute=True),
            market_cap_usd=(market_cap_thousand * 1000.0 if market_cap_thousand else None),
            timestamp=datetime.now(US_EASTERN),
        )

    def daily_bars(
        self,
        stock: StockInfo,
        *,
        calendar_days: int = 1200,
        max_rows: int = 650,
    ) -> pd.DataFrame:
        now = datetime.now(US_EASTERN)
        rows = self.paged(
            "usa06012",
            "/api/us/chart",
            {
                "stex_tp": stock.exchange,
                "stk_cd": stock.symbol,
                # usa06012 returns bars backwards from this anchor date.  A
                # past lower-bound here silently makes the whole result old.
                "strt_dt": now.strftime("%Y%m%d"),
                "upd_stkpc_tp": "1",
                "exrt_appl_tp": "0",
            },
            list_key="result_list",
            max_pages=12,
            max_rows=max_rows,
        )
        frame = _daily_frame(rows)
        cutoff = pd.Timestamp(now.date() - timedelta(days=max(180, calendar_days)))
        frame = frame.loc[frame.index >= cutoff]
        frame = completed_daily_bars(frame, now=now)
        if len(frame) < 80:
            raise KiwoomRestError(
                f"{stock.symbol} 일봉이 {len(frame)}개뿐이라 일목 분석에 부족합니다(최소 80개)."
            )
        if not daily_frame_is_current(frame, now=now):
            latest = pd.Timestamp(frame.index[-1]).date().isoformat()
            raise KiwoomRestError(
                f"{stock.symbol} 일봉의 마지막 날짜가 {latest}로 오래되었습니다. "
                "최신 데이터가 아니므로 분석을 중단합니다."
            )
        return frame.tail(max_rows)

    def minute_bars(
        self,
        stock: StockInfo,
        *,
        interval_minutes: int = 60,
        calendar_days: int = 120,
        max_rows: int = 500,
    ) -> pd.DataFrame:
        now = datetime.now(US_EASTERN)
        rows = self.paged(
            "usa06011",
            "/api/us/chart",
            {
                "stex_tp": stock.exchange,
                "stk_cd": stock.symbol,
                "strt_dt": now.strftime("%Y%m%d"),
                "tic_scope": str(interval_minutes),
                "upd_stkpc_tp": "1",
                "exrt_appl_tp": "0",
            },
            list_key="result_list",
            max_pages=12,
            max_rows=max_rows,
        )
        frame = _minute_frame(rows)
        cutoff = pd.Timestamp(now - timedelta(days=max(10, calendar_days)))
        frame = frame.loc[frame.index >= cutoff]
        if not frame.empty and not minute_frame_is_current(frame, now=now):
            latest = pd.Timestamp(frame.index[-1]).isoformat()
            raise KiwoomRestError(
                f"{stock.symbol} {interval_minutes}분봉의 마지막 시각이 {latest}로 오래되었습니다. "
                "최신 데이터가 아니므로 분석을 중단합니다."
            )
        return frame.tail(max_rows)

    def ranking(self, api_id: str, *, max_rows: int = 250) -> list[dict[str, Any]]:
        body: dict[str, str] = {
            "stex_tp": "0",
            "inds_cd": "",
            "stk_tp": "1",
            "trde_qty_tp": "0",
            "stk_cnd": "0",
            # Intraday rank filters reset as the US session opens.  Applying a
            # large server-side threshold here would wrongly remove liquid
            # stocks early in the day, so price/liquidity are checked locally
            # against the stock's 20 completed sessions instead.
            "pric_cnd": "0",
            "trde_prica_cnd": "0",
        }
        if api_id == "usa20530":
            body["qry_tp"] = "1"
        return self.paged(
            api_id,
            "/api/us/rkinfo",
            body,
            list_key="result_list",
            max_pages=5,
            max_rows=max_rows,
        )


def _daily_frame(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in rows:
        timestamp = pd.to_datetime(str(row.get("dt", "")), format="%Y%m%d", errors="coerce")
        values = {
            "timestamp": timestamp,
            "open": number(row.get("open_pric"), absolute=True),
            "high": number(row.get("high_pric"), absolute=True),
            "low": number(row.get("low_pric"), absolute=True),
            "close": number(row.get("cur_prc"), absolute=True),
            "volume": number(row.get("acc_trde_qty"), absolute=True),
            "trade_value": number(row.get("acc_trde_prica"), absolute=True),
        }
        if pd.notna(timestamp) and all(values[key] is not None for key in ("open", "high", "low", "close")):
            records.append(values)
    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "trade_value"])
    frame = pd.DataFrame.from_records(records).drop_duplicates("timestamp", keep="first")
    frame = frame.sort_values("timestamp").set_index("timestamp")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
    calculated_value = frame["close"] * frame["volume"]
    frame["trade_value"] = pd.to_numeric(frame["trade_value"], errors="coerce").fillna(calculated_value)
    return frame.astype(float)


def latest_completed_us_weekday(now: datetime | None = None) -> date:
    """Return the latest date whose US regular session can be complete.

    The API does not expose a trading-calendar endpoint.  Weekends are handled
    exactly; the recency check below permits a small calendar-day lag so US
    exchange holidays do not make a valid previous close unusable.
    """

    local = _eastern_datetime(now)
    session_date = local.date()
    if local.weekday() < 5 and local.hour * 60 + local.minute < REGULAR_SESSION_CLOSE_MINUTE:
        session_date -= timedelta(days=1)
    while session_date.weekday() >= 5:
        session_date -= timedelta(days=1)
    return session_date


def completed_daily_bars(
    frame: pd.DataFrame, *, now: datetime | None = None
) -> pd.DataFrame:
    """Remove today's still-forming daily bar before any close-based analysis."""

    if frame.empty:
        return frame.copy()
    completed_through = latest_completed_us_weekday(now)
    dates = pd.DatetimeIndex(frame.index).date
    return frame.loc[dates <= completed_through].copy()


def daily_frame_is_current(
    frame: pd.DataFrame, *, now: datetime | None = None
) -> bool:
    """Reject caches/API responses that are fresh on disk but stale in market time."""

    if frame.empty:
        return False
    latest = pd.Timestamp(frame.index[-1]).date()
    local = _eastern_datetime(now)
    expected = latest_completed_us_weekday(local)
    if latest > local.date():
        return False
    return latest >= expected - timedelta(days=MAX_DAILY_SESSION_LAG_DAYS)


def minute_frame_is_current(
    frame: pd.DataFrame, *, now: datetime | None = None
) -> bool:
    if frame.empty:
        return False
    local = _eastern_datetime(now)
    latest = pd.Timestamp(frame.index[-1])
    if latest.tzinfo is None:
        latest = latest.tz_localize(US_EASTERN)
    else:
        latest = latest.tz_convert(US_EASTERN)
    return local - timedelta(days=MAX_MINUTE_DATA_AGE_DAYS) <= latest <= local + timedelta(hours=1)


def _eastern_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(US_EASTERN)
    if value.tzinfo is None:
        return value.replace(tzinfo=US_EASTERN)
    return value.astimezone(US_EASTERN)


def _minute_frame(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in rows:
        timestamp = _parse_us_chart_time(row.get("cntr_tm"), row.get("bus_dt"))
        values = {
            "timestamp": timestamp,
            "open": number(row.get("open_pric"), absolute=True),
            "high": number(row.get("high_pric"), absolute=True),
            "low": number(row.get("low_pric"), absolute=True),
            "close": number(row.get("cur_prc"), absolute=True),
            "volume": number(row.get("trde_qty"), absolute=True),
        }
        if pd.notna(timestamp) and all(values[key] is not None for key in ("open", "high", "low", "close")):
            records.append(values)
    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame = pd.DataFrame.from_records(records).drop_duplicates("timestamp", keep="first")
    frame = frame.sort_values("timestamp").set_index("timestamp")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
    return frame.astype(float)


def _parse_us_chart_time(value: Any, business_date: Any = None) -> pd.Timestamp:
    """Parse Kiwoom's US-session clock, including 24:00-27:00 overnight bars."""

    text = re.sub(r"\D", "", str(value or ""))
    if len(text) < 14:
        return pd.NaT
    date_text = text[:8]
    if not date_text.isdigit():
        date_text = re.sub(r"\D", "", str(business_date or ""))[:8]
    base = pd.to_datetime(date_text, format="%Y%m%d", errors="coerce")
    try:
        hour, minute, second = int(text[8:10]), int(text[10:12]), int(text[12:14])
    except ValueError:
        return pd.NaT
    if pd.isna(base) or hour > 47 or minute > 59 or second > 59:
        return pd.NaT
    naive = base + pd.Timedelta(hours=hour, minutes=minute, seconds=second)
    return naive.tz_localize(US_EASTERN, nonexistent="shift_forward", ambiguous="NaT")


def regular_session_minute_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep only Kiwoom's hourly buckets that overlap the US regular session."""

    if frame.empty:
        return frame.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize(US_EASTERN)
    else:
        index = index.tz_convert(US_EASTERN)
    minutes = index.hour * 60 + index.minute
    # Kiwoom labels the opening partial bucket 09:00 and the closing bucket
    # 16:00.  Overnight/extended buckets (04-08, 17-27) are excluded.
    return frame.loc[(minutes >= 9 * 60) & (minutes <= 16 * 60)].copy()
