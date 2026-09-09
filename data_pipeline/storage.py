from __future__ import annotations

"""Content-addressed raw storage and evaluator-compatible processed output."""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Iterable

import pandas as pd

from .models import AssetManifest, DatasetManifest, QualityReport


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class HistoricalDataStore:
    def __init__(self, project_root: Path, dataset_id: str) -> None:
        self.project_root = Path(project_root).resolve()
        self.dataset_id = _safe_component(dataset_id)
        self.raw_root = self.project_root / "data" / "raw"
        self.metadata_root = self.project_root / "data" / "metadata"
        self.dataset_root = self.project_root / "evaluation" / "datasets" / self.dataset_id
        self.manifest_path = self.metadata_root / "manifests" / f"{self.dataset_id}.json"
        self.quality_json_path = self.metadata_root / "quality" / f"{self.dataset_id}.json"
        self.quality_csv_path = self.metadata_root / "quality" / f"{self.dataset_id}.csv"
        self.checkpoint_path = self.metadata_root / "checkpoints" / f"{self.dataset_id}.json"
        self.failure_path = self.metadata_root / "failures" / f"{self.dataset_id}.jsonl"

    def write_price_artifact(
        self,
        frame: pd.DataFrame,
        *,
        source: str,
        market: str,
        symbol: str,
        kind: str,
        publish_processed: bool = True,
    ) -> tuple[Path, Path, str]:
        market = market.upper()
        source_component = _safe_component(source)
        symbol_component = _safe_component(symbol)
        csv_bytes = _frame_csv_bytes(frame)
        data_hash = sha256_bytes(csv_bytes)
        raw_path = self.raw_root / market.lower() / source_component / kind / symbol_component / f"{data_hash}.csv"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        if not raw_path.exists():
            raw_path.write_bytes(csv_bytes)
        else:
            if sha256_file(raw_path) != data_hash:
                raise RuntimeError(f"기존 content-addressed 파일의 hash가 다릅니다: {raw_path}")
        folder = "proxies" if kind == "index" else "stocks"
        processed_path = self.dataset_root / market / folder / f"{symbol_component}.csv"
        if publish_processed:
            processed_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(processed_path, csv_bytes)
        return raw_path, processed_path, data_hash

    def write_stock_metadata(self, market: str, rows: Iterable[dict[str, object]]) -> Path:
        path = self.dataset_root / market.upper() / "stocks.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(list(rows))
        payload = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
        _atomic_write(path, payload)
        return path

    def write_universe_snapshots(self, rows: Iterable[dict[str, object]]) -> Path:
        path = self.dataset_root / "universe.csv"
        values = list(rows)
        required = ["date", "market", "symbol", "eligible", "source", "point_in_time_level"]
        frame = pd.DataFrame(values)
        for column in required:
            if column not in frame:
                frame[column] = pd.Series(dtype="object")
        extra = [column for column in frame.columns if column not in required]
        frame = frame.loc[:, required + extra]
        _atomic_write(path, frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))
        return path

    def write_source_snapshot(
        self,
        *,
        source: str,
        category: str,
        base_date: str,
        payload: bytes,
        suffix: str = "json",
    ) -> tuple[Path, str]:
        """Preserve an exact provider response without putting credentials in its path."""

        data_hash = sha256_bytes(payload)
        path = (
            self.raw_root
            / "kr"
            / _safe_component(source)
            / "snapshots"
            / _safe_component(category)
            / _safe_component(base_date)
            / f"{data_hash}.{_safe_component(suffix)}"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(payload)
        elif sha256_file(path) != data_hash:
            raise RuntimeError(f"기존 content-addressed 파일의 hash가 다릅니다: {path}")
        return path, data_hash

    def load_checkpoint(self) -> dict[str, object]:
        if not self.checkpoint_path.exists():
            return {"dataset_id": self.dataset_id, "completed": {}, "failed": {}}
        return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))

    def save_checkpoint(self, value: dict[str, object]) -> None:
        value["updated_at"] = _utc_now()
        _atomic_write(self.checkpoint_path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))

    def append_failure(self, value: dict[str, object]) -> None:
        self.failure_path.parent.mkdir(parents=True, exist_ok=True)
        with self.failure_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")

    def write_quality_reports(
        self, reports: Iterable[QualityReport], *, preserve_existing: bool = True
    ) -> tuple[Path, Path]:
        new_values = [item.to_dict() for item in reports]
        existing: list[dict[str, object]] = []
        if preserve_existing and self.quality_json_path.exists():
            try:
                loaded = json.loads(self.quality_json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    existing = [item for item in loaded if isinstance(item, dict)]
            except (OSError, ValueError):
                existing = []
        replaced = {(str(item.get("market")), str(item.get("symbol"))) for item in new_values}
        values = [
            item for item in existing
            if (str(item.get("market")), str(item.get("symbol"))) not in replaced
        ] + new_values
        _atomic_write(self.quality_json_path, json.dumps(values, ensure_ascii=False, indent=2).encode("utf-8"))
        rows: list[dict[str, object]] = []
        for report in values:
            base = {key: report[key] for key in ("market", "symbol", "checked_at", "rows", "status", "error_count", "warning_count")}
            issues = report.get("issues", [])
            if not issues:
                rows.append({**base, "severity": "", "code": "", "message": "", "count": 0, "examples": ""})
            for issue in issues:
                rows.append({**base, **issue, "examples": " | ".join(issue.get("examples", []))})
        frame = pd.DataFrame(rows)
        _atomic_write(self.quality_csv_path, frame.to_csv(index=False, lineterminator="\n").encode("utf-8-sig"))
        return self.quality_json_path, self.quality_csv_path

    def write_manifest(self, manifest: DatasetManifest) -> Path:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.manifest_path, json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"))
        index_path = self.metadata_root / "manifests" / "index.json"
        index = [] if not index_path.exists() else json.loads(index_path.read_text(encoding="utf-8"))
        index = [item for item in index if item.get("dataset_id") != manifest.dataset_id]
        index.append(
            {
                "dataset_id": manifest.dataset_id,
                "manifest_path": str(self.manifest_path.relative_to(self.project_root)),
                "updated_at": manifest.updated_at,
                "status": manifest.status,
                "survivorship_safe": manifest.survivorship_safe,
                "point_in_time_level": manifest.point_in_time_level,
                "asset_count": len(manifest.assets),
            }
        )
        index.sort(key=lambda item: str(item["dataset_id"]))
        _atomic_write(index_path, json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"))
        return self.manifest_path


def current_git_commit(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _frame_csv_bytes(frame: pd.DataFrame) -> bytes:
    output = frame.copy()
    index_name = output.index.name or "timestamp"
    output.index.name = index_name
    return output.to_csv(index=True, lineterminator="\n", date_format="%Y-%m-%dT%H:%M:%S%z").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _safe_component(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in {"-", "_", "."} else "_" for character in str(value).strip())
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"안전하지 않은 경로 구성요소: {value!r}")
    return cleaned


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
