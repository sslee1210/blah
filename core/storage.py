from __future__ import annotations

"""Atomic local cache isolated from the domestic Real analyzer."""

import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .kiwoom_rest import StockInfo


US_EASTERN = ZoneInfo("America/New_York")


class CacheStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.daily_dir = self.root / "daily"
        self.minute_dir = self.root / "minute"
        self.root.mkdir(parents=True, exist_ok=True)
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.minute_dir.mkdir(parents=True, exist_ok=True)

    def _stock_key(self, stock: StockInfo) -> str:
        symbol = re.sub(r"[^A-Z0-9._-]", "_", stock.symbol.upper())
        return f"{stock.exchange}_{symbol}"

    def daily_path(self, stock: StockInfo) -> Path:
        return self.daily_dir / f"{self._stock_key(stock)}.csv"

    def minute_path(self, stock: StockInfo, interval: int) -> Path:
        return self.minute_dir / f"{self._stock_key(stock)}_{interval}m.csv"

    def load_daily(self, stock: StockInfo, *, fresh_only: bool = False) -> pd.DataFrame | None:
        path = self.daily_path(stock)
        if not path.exists() or (fresh_only and not self._fresh(path, intraday=False)):
            return None
        return self._read_frame(path)

    def save_daily(self, stock: StockInfo, frame: pd.DataFrame) -> None:
        self._write_frame(self.daily_path(stock), frame)

    def load_minute(
        self, stock: StockInfo, interval: int, *, fresh_only: bool = False
    ) -> pd.DataFrame | None:
        path = self.minute_path(stock, interval)
        if not path.exists() or (fresh_only and not self._fresh(path, intraday=True)):
            return None
        return self._read_frame(path)

    def save_minute(self, stock: StockInfo, interval: int, frame: pd.DataFrame) -> None:
        self._write_frame(self.minute_path(stock, interval), frame)

    def load_master(self, *, max_age_hours: int = 24) -> list[StockInfo] | None:
        path = self.root / "us_stock_master.json"
        if not path.exists():
            return None
        age = datetime.now().timestamp() - path.stat().st_mtime
        if age > max_age_hours * 3600:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [StockInfo(**item) for item in payload if isinstance(item, dict)]
        except (OSError, TypeError, ValueError):
            return None

    def save_master(self, stocks: list[StockInfo]) -> None:
        path = self.root / "us_stock_master.json"
        payload = [stock.__dict__ for stock in stocks]
        self._atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def save_scan_state(self, payload: dict[str, Any]) -> None:
        path = self.root / "last_scan.json"
        self._atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    def _fresh(self, path: Path, *, intraday: bool) -> bool:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=US_EASTERN)
        now = datetime.now(US_EASTERN)
        weekday = now.weekday() < 5
        minutes = now.hour * 60 + now.minute
        regular_session = weekday and 570 <= minutes <= 975
        if intraday:
            maximum_age = timedelta(minutes=10 if regular_session else 90)
        else:
            maximum_age = timedelta(minutes=15 if regular_session else 12 * 60)
        return now - modified <= maximum_age

    @staticmethod
    def _read_frame(path: Path) -> pd.DataFrame | None:
        try:
            frame = pd.read_csv(path, index_col="timestamp", parse_dates=["timestamp"])
            frame.index.name = "timestamp"
            return frame.sort_index()
        except (OSError, ValueError, pd.errors.ParserError):
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
            if temp.exists():
                temp.unlink()

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
            if temp.exists():
                temp.unlink()

