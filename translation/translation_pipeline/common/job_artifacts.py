"""Job-scoped local artifact paths for translation debugging output."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "tmp" / "job_artifacts"
_JOB_DIR_CACHE: dict[tuple[str, str], Path] = {}


def _root() -> Path:
    value = os.getenv("AI_TRANSLATION_JOB_ARTIFACT_ROOT", "").strip()
    return Path(value) if value else _DEFAULT_ROOT


def safe_artifact_part(value: Any, *, limit: int = 96) -> str:
    safe = re.sub(r"[^0-9A-Za-z가-힣_.() -]+", "_", str(value or "").strip())
    safe = re.sub(r"\s+", "_", safe).strip("._- ")
    return safe[:limit]


def _timestamp() -> str:
    try:
        now = datetime.now(ZoneInfo("Asia/Seoul"))
    except Exception:  # pragma: no cover
        now = datetime.now()
    return now.strftime("%y%m%d_%H%M%S")


def job_artifact_dir(job_id: str, artifact_label: str = "") -> Path:
    safe_job = safe_artifact_part(job_id, limit=64) or "unknown-job"
    safe_artifact = safe_artifact_part(artifact_label, limit=96)
    cache_key = (safe_job, safe_artifact)
    cached = _JOB_DIR_CACHE.get(cache_key)
    if cached:
        return cached

    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    existing = sorted(root.glob(f"*__{safe_job}"))
    if existing:
        path = existing[-1]
    else:
        parts = [_timestamp()]
        if safe_artifact:
            parts.append(safe_artifact)
        parts.append(safe_job)
        path = root / "__".join(parts)
        path.mkdir(parents=True, exist_ok=True)

    _JOB_DIR_CACHE[cache_key] = path
    return path


def job_artifact_path(
    job_id: str,
    artifact_label: str,
    filename: str,
    *,
    subdir: str = "",
) -> Path:
    base = job_artifact_dir(job_id, artifact_label)
    if subdir:
        base = base / safe_artifact_part(subdir, limit=80)
    base.mkdir(parents=True, exist_ok=True)
    return base / filename


def next_numbered_artifact_path(
    job_id: str,
    artifact_label: str,
    *,
    subdir: str,
    stem: str,
    suffix: str = ".json",
) -> Path:
    directory = job_artifact_path(job_id, artifact_label, ".keep", subdir=subdir).parent
    pattern = re.compile(rf"^{re.escape(stem)}_(\d+){re.escape(suffix)}$")
    max_index = 0
    for path in directory.glob(f"{stem}_*{suffix}"):
        match = pattern.match(path.name)
        if match:
            max_index = max(max_index, int(match.group(1)))
    return directory / f"{stem}_{max_index + 1:03d}{suffix}"
