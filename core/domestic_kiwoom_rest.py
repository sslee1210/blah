from __future__ import annotations

"""Kiwoom REST helpers for KOSPI/KOSDAQ market data."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from .kiwoom_rest import InvalidSecurityError, KiwoomRestClient, KiwoomRestError, StockInfo, number


KOREA = ZoneInfo("Asia/Seoul")
DOMESTIC_SESSION_CLOSE_MINUTE = 15 * 60 + 30
MAX_DAILY_SESSION_LAG_DAYS = 4
MAX_MINUTE_DATA_AGE_DAYS = 7
MARKET_TYPES = {"0": "KOSPI", "10": "KOSDAQ"}


class InsufficientDomesticHistoryError(KiwoomRestError):
    """A listed stock does not yet have enough completed bars for Ichimoku."""


@dataclass(frozen=True)
class DomesticQuote:
    symbol: str
    exchange: str
    name: str
    price: float
    open: float | None
    high: float | None
    low: float | None
    volume: float | None
    prev_close: float | None
    timestamp: datetime
    currency: str = "KRW"


class DomesticKiwoomRestClient(KiwoomRestClient):
    """Domestic-market methods on top of the shared Kiwoom auth/request layer."""

    def stock_master(self) -> list[StockInfo]:
        result: dict[tuple[str, str], StockInfo] = {}
        for market_type, exchange in MARKET_TYPES.items():
            rows = self.paged(
                "ka10099",
                "/api/dostk/stkinfo",
                {"mrkt_tp": market_type},
                list_key="list",
                max_pages=10,
            )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = _base_symbol(row.get("code"))
                name = str(row.get("name", "")).strip()
                stock = StockInfo(
                    symbol=symbol,
                    exchange=exchange,
                    korean_name=name,
                    english_name="",
                    sector=str(row.get("upName", "")).strip() or "미분류",
                    is_etf=False,
                )
                if is_supported_domestic_stock(
                    stock,
                    market_name=str(row.get("marketName", "")),
                    audit_info=str(row.get("auditInfo", "")),
                    state=str(row.get("state", "")),
                    order_warning=str(row.get("orderWarning", "0")),
                ):
                    result[(exchange, symbol)] = stock
        return list(result.values())

    def quote(self, stock: StockInfo) -> DomesticQuote:
        payload, _ = self.request(
            "ka10001",
            "/api/dostk/stkinfo",
            {"stk_cd": stock.symbol},
        )
        price = number(payload.get("cur_prc"), absolute=True)
        if price is None or price <= 0:
            raise KiwoomRestError(f"{stock.symbol} 현재가가 비어 있습니다.")
        return DomesticQuote(
            symbol=stock.symbol,
            exchange=stock.exchange,
            name=str(payload.get("stk_nm", "")).strip() or stock.display_name,
            price=price,
            open=number(payload.get("open_pric"), absolute=True),
            high=number(payload.get("high_pric"), absolute=True),
            low=number(payload.get("low_pric"), absolute=True),
            volume=number(payload.get("trde_qty"), absolute=True),
            prev_close=number(payload.get("base_pric"), absolute=True),
            timestamp=datetime.now(KOREA),
        )

    def daily_bars(self, stock: StockInfo, *, max_rows: int = 650) -> pd.DataFrame:
        now = datetime.now(KOREA)
        rows = self.paged(
            "ka10081",
            "/api/dostk/chart",
            {
                "stk_cd": stock.symbol,
                "base_dt": now.strftime("%Y%m%d"),
                "upd_stkpc_tp": "1",
            },
            list_key="stk_dt_pole_chart_qry",
            max_pages=4,
            max_rows=max_rows,
        )
        frame = _domestic_daily_frame(rows)
        frame = completed_domestic_daily_bars(frame, now=now)
        if len(frame) < 80:
            raise InsufficientDomesticHistoryError(
                f"{stock.symbol} 일봉이 {len(frame)}개뿐이라 일목 분석에 부족합니다(최소 80개)."
            )
        if not domestic_daily_frame_is_current(frame, now=now):
            latest = pd.Timestamp(frame.index[-1]).date().isoformat()
            raise KiwoomRestError(
                f"{stock.symbol} 국내 일봉의 마지막 날짜가 {latest}로 오래되었습니다. "
                "최신 완료 일봉이 아니므로 분석을 중단합니다."
            )
        return frame.tail(max_rows)

    def minute_bars(
        self,
        stock: StockInfo,
        *,
        interval_minutes: int = 60,
        max_rows: int = 500,
    ) -> pd.DataFrame:
        rows = self.paged(
            "ka10080",
            "/api/dostk/chart",
            {
                "stk_cd": stock.symbol,
                "tic_scope": str(interval_minutes),
                "upd_stkpc_tp": "1",
            },
            list_key="stk_min_pole_chart_qry",
            max_pages=5,
            max_rows=max_rows,
        )
        frame = regular_session_domestic_minute_bars(_domestic_minute_frame(rows))
        frame = completed_domestic_minute_bars(
            frame,
            interval_minutes=interval_minutes,
        )
        if len(frame) < 80:
            raise KiwoomRestError(
                f"{stock.symbol} 국내 {interval_minutes}분봉 데이터가 {len(frame)}개뿐이라 부족합니다."
            )
        if not domestic_minute_frame_is_current(frame):
            latest = pd.Timestamp(frame.index[-1]).isoformat()
            raise KiwoomRestError(
                f"{stock.symbol} 국내 {interval_minutes}분봉의 마지막 시각이 {latest}로 오래되었습니다."
            )
        return frame.tail(max_rows)

    def index_daily_bars(self, index_code: str, *, max_rows: int = 600) -> pd.DataFrame:
        now = datetime.now(KOREA)
        rows = self.paged(
            "ka20006",
            "/api/dostk/chart",
            {"inds_cd": index_code, "base_dt": now.strftime("%Y%m%d")},
            list_key="inds_dt_pole_qry",
            max_pages=3,
            max_rows=max_rows,
        )
        frame = completed_domestic_daily_bars(_domestic_daily_frame(rows), now=now)
        if len(frame) < 80:
            raise KiwoomRestError(f"국내 업종지수 {index_code} 일봉 데이터가 부족합니다.")
        if not domestic_daily_frame_is_current(frame, now=now):
            raise KiwoomRestError(f"국내 업종지수 {index_code} 완료 일봉이 오래되었습니다.")
        return frame.tail(max_rows)

    def trading_value_top(self, market: str, *, max_rows: int = 150) -> list[dict[str, Any]]:
        if market not in {"001", "101"}:
            raise ValueError("거래대금 순위 시장은 001(KOSPI) 또는 101(KOSDAQ)이어야 합니다.")
        return self.paged(
            "ka10032",
            "/api/dostk/rkinfo",
            {
                "mrkt_tp": market,
                # Official TR docs define 1 as management-stock exclusion.
                # The master filter below is still the final safety gate.
                "mang_stk_incls": "1",
                # KRX only. Integrated SOR can return suffixes such as _AL,
                # which would duplicate the same listed company in a scan.
                "stex_tp": "1",
            },
            list_key="trde_prica_upper",
            max_pages=4,
            max_rows=max_rows,
        )


def is_supported_domestic_stock(
    stock: StockInfo,
    *,
    market_name: str = "",
    audit_info: str = "",
    state: str = "",
    order_warning: str = "0",
) -> bool:
    if stock.exchange not in {"KOSPI", "KOSDAQ"} or stock.is_etf:
        return False
    # Kiwoom now returns valid listed share codes containing letters as well
    # as legacy six-digit numeric codes. Preserve them as strings.
    if not re.fullmatch(r"[0-9A-Z]{6}", stock.symbol.upper()):
        return False
    if market_name:
        expected_market = "거래소" if stock.exchange == "KOSPI" else "코스닥"
        if market_name.strip() != expected_market:
            return False
    name = stock.display_name.strip()
    if not name:
        return False
    if re.search(r"(?:스팩|SPAC|기업인수목적)", name, re.IGNORECASE):
        return False
    if re.search(r"(?:우|우B|우C|[1-9]우B|[1-9]우C|우선주)$", name, re.IGNORECASE):
        return False
    if re.search(r"(?:ETF|ETN)$", name, re.IGNORECASE):
        return False
    warning = str(order_warning).strip()
    if warning and warning != "0":
        return False
    risk_text = f"{audit_info} {state}"
    if any(term in risk_text for term in ("관리종목", "정리매매", "거래정지", "투자위험")):
        return False
    return True


def _base_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.split("_", 1)[0]


def _domestic_daily_frame(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise KiwoomRestError("국내 일봉 OHLCV 응답 형식이 올바르지 않습니다.")
        timestamp = pd.to_datetime(str(row.get("dt", "")), format="%Y%m%d", errors="coerce")
        open_price = number(row.get("open_pric"), absolute=True)
        high = number(row.get("high_pric"), absolute=True)
        low = number(row.get("low_pric"), absolute=True)
        close = number(row.get("cur_prc"), absolute=True)
        volume = _domestic_volume(row)
        _validate_domestic_bar(timestamp, open_price, high, low, close)
        # Kiwoom's domestic trde_prica field is scaled. Calculate KRW turnover
        # from adjusted close * shares so liquidity thresholds have one unit.
        records.append(
            {
                "timestamp": timestamp,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "trade_value": float(close) * volume if volume is not None else None,
            }
        )
    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "trade_value"])
    frame = pd.DataFrame.from_records(records).drop_duplicates("timestamp", keep="first")
    return frame.sort_values("timestamp").set_index("timestamp").astype(float)


def _domestic_minute_frame(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise KiwoomRestError("국내 분봉 OHLCV 응답 형식이 올바르지 않습니다.")
        timestamp = _parse_domestic_chart_time(row.get("cntr_tm"))
        open_price = number(row.get("open_pric"), absolute=True)
        high = number(row.get("high_pric"), absolute=True)
        low = number(row.get("low_pric"), absolute=True)
        close = number(row.get("cur_prc"), absolute=True)
        volume = _domestic_volume(row)
        _validate_domestic_bar(timestamp, open_price, high, low, close)
        records.append(
            {
                "timestamp": timestamp,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "trade_value": float(close) * volume if volume is not None else None,
            }
        )
    if not records:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "trade_value"])
    frame = pd.DataFrame.from_records(records).drop_duplicates("timestamp", keep="first")
    return frame.sort_values("timestamp").set_index("timestamp").astype(float)


def _domestic_volume(row: dict[str, Any]) -> float | None:
    raw = row.get("trde_qty")
    volume = number(raw, absolute=True)
    if raw is not None and str(raw).strip() and volume is None:
        raise KiwoomRestError("국내 차트 OHLCV 거래량 형식이 올바르지 않습니다.")
    return volume


def _validate_domestic_bar(
    timestamp: pd.Timestamp,
    open_price: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
) -> None:
    prices = (open_price, high, low, close)
    if pd.isna(timestamp) or any(value is None or value <= 0 for value in prices):
        raise KiwoomRestError("국내 차트 OHLCV 날짜 또는 가격이 비어 있거나 올바르지 않습니다.")
    if high < max(open_price, close, low) or low > min(open_price, close, high):
        raise KiwoomRestError("국내 차트 OHLCV 고가·저가와 시가·종가의 관계가 올바르지 않습니다.")


def _parse_domestic_chart_time(value: Any) -> pd.Timestamp:
    text = re.sub(r"\D", "", str(value or ""))
    if len(text) < 14:
        return pd.NaT
    parsed = pd.to_datetime(text[:14], format="%Y%m%d%H%M%S", errors="coerce")
    if pd.isna(parsed):
        return pd.NaT
    return parsed.tz_localize(KOREA, nonexistent="shift_forward", ambiguous="NaT")


def latest_completed_domestic_weekday(now: datetime | None = None) -> date:
    local = _korea_datetime(now)
    session_date = local.date()
    minutes = local.hour * 60 + local.minute
    if local.weekday() < 5 and minutes < DOMESTIC_SESSION_CLOSE_MINUTE:
        session_date -= timedelta(days=1)
    while session_date.weekday() >= 5:
        session_date -= timedelta(days=1)
    return session_date


def completed_domestic_daily_bars(
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    completed_through = latest_completed_domestic_weekday(now)
    dates = pd.DatetimeIndex(frame.index).date
    return frame.loc[dates <= completed_through].copy()


def domestic_daily_frame_is_current(
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> bool:
    if frame.empty:
        return False
    latest = pd.Timestamp(frame.index[-1]).date()
    local = _korea_datetime(now)
    expected = latest_completed_domestic_weekday(local)
    if latest > expected:
        return False
    return latest >= expected - timedelta(days=MAX_DAILY_SESSION_LAG_DAYS)


def domestic_minute_frame_is_current(
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> bool:
    if frame.empty:
        return False
    local = _korea_datetime(now)
    latest = pd.Timestamp(frame.index[-1])
    if latest.tzinfo is None:
        latest = latest.tz_localize(KOREA)
    else:
        latest = latest.tz_convert(KOREA)
    return local - timedelta(days=MAX_MINUTE_DATA_AGE_DAYS) <= latest <= local


def regular_session_domestic_minute_bars(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize(KOREA)
    else:
        index = index.tz_convert(KOREA)
    minutes = index.hour * 60 + index.minute
    return frame.loc[(minutes >= 9 * 60) & (minutes <= DOMESTIC_SESSION_CLOSE_MINUTE)].copy()


def completed_domestic_minute_bars(
    frame: pd.DataFrame,
    *,
    interval_minutes: int,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Remove the still-forming current intraday bucket.

    Kiwoom labels 60-minute bars by bucket start. The final 15:00 bucket ends
    at the domestic regular-session close (15:30), not at 16:00.
    """

    if interval_minutes <= 0:
        raise ValueError("분봉 간격은 양수여야 합니다.")
    if frame.empty:
        return frame.copy()
    local_now = _korea_datetime(now)
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize(KOREA)
    else:
        index = index.tz_convert(KOREA)
    keep: list[bool] = []
    for stamp in index:
        local_stamp = stamp.to_pydatetime()
        session_close = local_stamp.replace(hour=15, minute=30, second=0, microsecond=0)
        bucket_end = min(local_stamp + timedelta(minutes=interval_minutes), session_close)
        keep.append(bucket_end <= local_now)
    return frame.loc[keep].copy()


def _korea_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(KOREA)
    if value.tzinfo is None:
        return value.replace(tzinfo=KOREA)
    return value.astimezone(KOREA)
