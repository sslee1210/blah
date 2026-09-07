from __future__ import annotations

"""Local cache for Kiwoom domestic-market data."""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .domestic_kiwoom_rest import (
    KOREA,
    completed_domestic_daily_bars,
    completed_domestic_minute_bars,
    domestic_daily_frame_is_current,
    domestic_minute_frame_is_current,
    latest_completed_domestic_weekday,
    regular_session_domestic_minute_bars,
)
from .ichimoku import prepare_ohlcv
from .kiwoom_rest import StockInfo


MASTER_CACHE_VERSION = 2


class DomesticCacheStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.daily_dir = self.root / "daily"
        self.minute_dir = self.root / "minute"
        self.root.mkdir(parents=True, exist_ok=True)
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.minute_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _stock_key(stock: StockInfo) -> str:
        symbol = re.sub(r"[^0-9A-Z._-]", "_", stock.symbol.upper())
        exchange = re.sub(r"[^0-9A-Z._-]", "_", stock.exchange.upper())
        return f"{exchange}_{symbol}"

    def daily_path(self, stock: StockInfo) -> Path:
        return self.daily_dir / f"{self._stock_key(stock)}.csv"

    def minute_path(self, stock: StockInfo, interval: int) -> Path:
        return self.minute_dir / f"{self._stock_key(stock)}_{interval}m.csv"

    def load_daily(self, stock: StockInfo, *, fresh_only: bool = False) -> pd.DataFrame | None:
        path = self.daily_path(stock)
        if not path.exists():
            return None
        frame = self._read_frame(path)
        if frame is None:
            return None
        if fresh_only:
            now = datetime.now(KOREA)
            frame = completed_domestic_daily_bars(frame, now=now)
            if not _daily_cache_is_current(path, frame, now=now):
                return None
        return frame

    def save_daily(self, stock: StockInfo, frame: pd.DataFrame) -> None:
        self._write_frame(self.daily_path(stock), frame)

    def load_minute(
        self,
        stock: StockInfo,
        interval: int,
        *,
        fresh_only: bool = False,
    ) -> pd.DataFrame | None:
        path = self.minute_path(stock, interval)
        if not path.exists():
            return None
        frame = self._read_frame(path)
        if frame is None:
            return None
        if fresh_only:
            now = datetime.now(KOREA)
            frame = completed_domestic_minute_bars(
                regular_session_domestic_minute_bars(frame), interval_minutes=interval, now=now
            )
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=KOREA)
            minutes = now.hour * 60 + now.minute
            regular = now.weekday() < 5 and 9 * 60 <= minutes < 15 * 60 + 30
            maximum_age = timedelta(minutes=10 if regular else 120)
            if now - modified > maximum_age or not domestic_minute_frame_is_current(frame, now=now):
                return None
        return frame

    def save_minute(self, stock: StockInfo, interval: int, frame: pd.DataFrame) -> None:
        self._write_frame(self.minute_path(stock, interval), frame)

    def load_master(self, *, max_age_hours: int | None = 24) -> list[StockInfo] | None:
        path = self.root / "domestic_stock_master.json"
        if not path.exists():
            return None
        if max_age_hours is not None:
            age = datetime.now().timestamp() - path.stat().st_mtime
            if age > max_age_hours * 3600:
                return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != MASTER_CACHE_VERSION:
                return None
            stocks = payload.get("stocks", [])
            if not isinstance(stocks, list):
                return None
            return [StockInfo(**item) for item in stocks if isinstance(item, dict)]
        except (OSError, TypeError, ValueError):
            return None

    def save_master(self, stocks: list[StockInfo]) -> None:
        path = self.root / "domestic_stock_master.json"
        payload = {
            "version": MASTER_CACHE_VERSION,
            "stocks": [item.__dict__ for item in stocks],
        }
        self._atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def save_scan_state(self, payload: dict[str, Any]) -> None:
        self._atomic_text(
            self.root / "last_scan.json",
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        )

    @staticmethod
    def _read_frame(path: Path) -> pd.DataFrame | None:
        try:
            frame = pd.read_csv(path, index_col="timestamp")
            frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, errors="raise"))
            frame.index.name = "timestamp"
            return prepare_ohlcv(frame)
        except (OSError, TypeError, ValueError, pd.errors.ParserError):
            return None

    @staticmethod
    def _write_frame(path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        output = frame.copy()
        output.index.name = "timestamp"
        handle, temp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        os.close(handle)
        temp = Path(temp_name)
        try:
            output.to_csv(temp, encoding="utf-8-sig")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        os.close(handle)
        temp = Path(temp_name)
        try:
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _daily_cache_is_current(
    path: Path,
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a cached completed-day frame can be reused.

    A frame reaching the prior completed weekday can be reused over a weekend.
    If that weekday is missing, require a refresh written after its close.
    An exchange holiday can then reuse the unchanged, freshly fetched frame
    without repeatedly querying, while a missed working day triggers a fetch.
    """

    local = now or datetime.now(KOREA)
    if local.tzinfo is None:
        local = local.replace(tzinfo=KOREA)
    else:
        local = local.astimezone(KOREA)
    if not domestic_daily_frame_is_current(frame, now=local):
        return False
    expected = latest_completed_domestic_weekday(local)
    latest = pd.Timestamp(frame.index[-1]).date()
    if latest >= expected:
        return True
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=KOREA)
    close_time = datetime.combine(expected, datetime.min.time(), tzinfo=KOREA).replace(
        hour=15, minute=30
    )
    return modified >= close_time
