"""SSE event helpers for Office translation jobs."""

from __future__ import annotations

from typing import Any

from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.translation_jobs import publish_translation_event


def log_stream_event(event_name: str, payload: dict[str, Any]) -> None:
    progress_parts = []
    if payload.get("current_slide") is not None or payload.get("total_slides") is not None:
        progress_parts.append(f"slide={payload.get('current_slide')}/{payload.get('total_slides')}")
    if payload.get("current_page") is not None or payload.get("total_pages") is not None:
        progress_parts.append(f"page={payload.get('current_page')}/{payload.get('total_pages')}")
    if payload.get("current_sheet") is not None or payload.get("total_sheets") is not None:
        sheet_name = payload.get("current_sheet_name")
        sheet_label = f" sheet_name={sheet_name}" if sheet_name else ""
        progress_parts.append(f"sheet={payload.get('current_sheet')}/{payload.get('total_sheets')}{sheet_label}")
    if payload.get("translated_preview_html_url"):
        progress_parts.append("html=ready")
    if payload.get("progress_percent") is not None:
        progress_parts.append(
            "progress="
            f"{payload.get('progress_percent')}% "
            f"{payload.get('progress_completed')}/{payload.get('progress_total')} "
            f"unit={payload.get('progress_unit')}"
        )
    if payload.get("eta_ms") is not None:
        progress_parts.append(f"eta_ms={payload.get('eta_ms')}")
    suffix = f" ({', '.join(progress_parts)})" if progress_parts else ""
    log_info(f"[Office SSE] {event_name}{suffix}")


def publish_office_translation_event(job_id: str, event_name: str, payload: dict[str, Any]) -> None:
    log_stream_event(event_name, payload)
    publish_translation_event(job_id, event_name, payload)
