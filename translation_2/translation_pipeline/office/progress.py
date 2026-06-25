"""Progress and ETA helpers for Office translation pipelines."""

from __future__ import annotations

import time
from typing import Any

from translation_pipeline.common.logging_utils import log_info


def build_progress_payload(
    *,
    unit_kind: str,
    completed_units: int,
    total_units: int,
    started_at: float,
    current_label: str = "",
) -> dict[str, Any]:
    """Build common progress/ETA fields for slide, sheet, or char units."""

    total = max(0, int(total_units or 0))
    completed = max(0, int(completed_units or 0))
    if total:
        completed = min(completed, total)
    elapsed_ms = int(max(0.0, time.perf_counter() - started_at) * 1000)
    progress_ratio = (completed / total) if total > 0 else 0.0
    progress_percent = round(progress_ratio * 100.0, 1)
    eta_ms: int | None = None
    if completed > 0 and total > completed:
        avg_ms_per_unit = elapsed_ms / completed
        eta_ms = int(avg_ms_per_unit * (total - completed))

    payload: dict[str, Any] = {
        "progress_unit": unit_kind,
        "progress_completed": completed,
        "progress_total": total or None,
        "progress_percent": progress_percent,
        "eta_ms": eta_ms,
        "progress_elapsed_ms": elapsed_ms,
    }
    if current_label:
        payload["progress_current_label"] = current_label
    return {key: value for key, value in payload.items() if value is not None}


def log_progress(event_name: str, progress: dict[str, Any]) -> None:
    if not progress:
        return
    eta_ms = progress.get("eta_ms")
    eta_part = f" eta_ms={eta_ms}" if eta_ms is not None else " eta=calculating"
    label = progress.get("progress_current_label")
    label_part = f" label={label}" if label else ""
    log_info(
        "[Office progress] "
        f"{event_name} "
        f"unit={progress.get('progress_unit')} "
        f"completed={progress.get('progress_completed')}/{progress.get('progress_total')} "
        f"percent={progress.get('progress_percent')}"
        f"{eta_part}{label_part}"
    )


def build_initial_overall_progress_payload(
    *,
    ext: str,
    total_slides: int,
    total_sheets: int,
    docx_total_chars: int,
    started_at: float,
) -> dict[str, Any]:
    if ext == ".pptx":
        return build_progress_payload(
            unit_kind="slide",
            completed_units=0,
            total_units=total_slides,
            started_at=started_at,
            current_label="번역 대기",
        )
    if ext == ".xlsx":
        return build_progress_payload(
            unit_kind="sheet",
            completed_units=0,
            total_units=total_sheets,
            started_at=started_at,
            current_label="번역 대기",
        )
    if ext == ".docx":
        return build_progress_payload(
            unit_kind="char",
            completed_units=0,
            total_units=docx_total_chars,
            started_at=started_at,
            current_label="문서 번역 대기",
        )
    return {}
