"""Office 번역용 temporary glossary / pre-analysis / DTM setup."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List

from translation_pipeline.common.document_profile import (
    document_profile_enabled,
    get_static_document_profile,
)
from translation_pipeline.common.bilingual_summary_memory import (
    create_bilingual_summary_memory,
    save_bilingual_summary_memory_to_local_file,
)
from translation_pipeline.common.document_term_memory import (
    create_document_term_memory,
    document_term_memory_summary,
    save_document_term_memory_to_local_file,
)
from translation_pipeline.common.document_term_memory_pre_judge import (
    run_document_term_pre_judge,
)
from translation_pipeline.common.logging_utils import log_info
from translation_pipeline.common.pre_translation_analysis import (
    run_pre_translation_analysis,
    save_initial_glossary_to_local_file,
    save_pre_analysis_to_local_file,
)
from translation_pipeline.common.term_extractor import scan_terms
from translation_pipeline.common.term_memory_store import (
    create_memory,
    glossary_enabled,
    memory_summary,
    update_memory_from_scan,
)

from .types import InjectionUnit, TranslationUnit


@dataclass(slots=True)
class TranslationMemorySetup:
    """번역 시작 전 구성된 메모리 상태."""

    style_options: Dict[str, Any] | None
    temporary_glossary: dict[str, Any] | None
    pre_translation_analysis: dict[str, Any] | None
    document_term_memory: dict[str, Any] | None
    bilingual_summary_memory: dict[str, Any] | None


def temporary_glossary_memory(style_options: Dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(style_options, dict):
        return None
    memory = style_options.get("_temporary_glossary_memory")
    return memory if isinstance(memory, dict) else None


def document_term_memory(style_options: Dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(style_options, dict):
        return None
    memory = style_options.get("_document_term_memory_memory")
    return memory if isinstance(memory, dict) else None


def bilingual_summary_memory(style_options: Dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(style_options, dict):
        return None
    memory = style_options.get("_bilingual_summary_memory_memory")
    return memory if isinstance(memory, dict) else None


def _style_options_with_source_document_profile(
    style_options: Dict[str, Any] | None,
    target_lang: str,
) -> Dict[str, Any] | None:
    effective_style_options = style_options
    if (
        document_profile_enabled(style_options)
        and not (
            isinstance(effective_style_options, dict)
            and isinstance(effective_style_options.get("_source_document_profile"), dict)
        )
    ):
        source_document_profile = get_static_document_profile(
            style_options,
            target_lang=target_lang,
        )
        if source_document_profile:
            effective_style_options = {
                **(effective_style_options or {}),
                "_source_document_profile": source_document_profile,
            }
    return effective_style_options


async def setup_translation_memory(
    sem: Any,
    session: Any,
    injection_units: List[InjectionUnit],
    translation_units: List[TranslationUnit],
    target_lang: str,
    style_options: Dict[str, Any] | None,
    *,
    on_pre_translation_analysis: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_document_term_memory_update: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> TranslationMemorySetup:
    """번역 전 source profile, temporary glossary, pre-analysis, DTM을 준비한다."""

    effective_style_options = _style_options_with_source_document_profile(style_options, target_lang)
    temporary_glossary: dict[str, Any] | None = temporary_glossary_memory(style_options)
    term_memory: dict[str, Any] | None = document_term_memory(style_options)
    summary_memory: dict[str, Any] | None = bilingual_summary_memory(style_options)
    style_job_id = (
        effective_style_options.get("_job_id")
        if isinstance(effective_style_options, dict)
        else ""
    )
    artifact_label = (
        str(effective_style_options.get("_filename") or effective_style_options.get("_file_name") or "")
        if isinstance(effective_style_options, dict)
        else ""
    )

    if not glossary_enabled(style_options):
        if summary_memory is None:
            scopes = {
                unit.context_scope or f"unit:{unit.translation_unit_id}"
                for unit in translation_units
            }
            doc_format = "office"
            if any(scope.startswith("docx:") for scope in scopes):
                doc_format = "docx"
            elif any(scope.startswith("pptx:") for scope in scopes):
                doc_format = "pptx"
            elif any(scope.startswith("xlsx:") for scope in scopes):
                doc_format = "xlsx"
            summary_memory = create_bilingual_summary_memory(
                job_id=str(style_job_id or ""),
                target_lang=target_lang,
                doc_format=doc_format,
                translation_units=translation_units,
                style_options=effective_style_options,
            )
            if summary_memory:
                summary_path = save_bilingual_summary_memory_to_local_file(
                    str(style_job_id or ""),
                    summary_memory,
                    artifact_label=artifact_label,
                )
                summary_memory["_dump_path"] = summary_path or None
                effective_style_options = {
                    **(effective_style_options or {}),
                    "_bilingual_summary_memory_memory": summary_memory,
                }
        return TranslationMemorySetup(
            style_options=effective_style_options,
            temporary_glossary=temporary_glossary,
            pre_translation_analysis=None,
            document_term_memory=term_memory,
            bilingual_summary_memory=summary_memory,
        )

    memory_stage_start = time.perf_counter()
    pre_analysis: dict[str, Any] | None = None
    if temporary_glossary is None:
        scan_stage_start = time.perf_counter()
        temporary_glossary = create_memory(target_lang=target_lang)
        scan_result = scan_terms(
            translation_units,
            injection_units,
            target_lang=target_lang,
        )
        update_memory_from_scan(temporary_glossary, scan_result)
        log_info(
            "[Temporary Glossary] scan complete "
            f"{memory_summary(temporary_glossary)} "
            f"elapsed={time.perf_counter() - scan_stage_start:.2f}s"
        )
    effective_style_options = {
        **(effective_style_options or {}),
        "_temporary_glossary_memory": temporary_glossary,
    }
    analysis_stage_start = time.perf_counter()
    pre_analysis = await run_pre_translation_analysis(
        sem,
        session,
        temporary_glossary,
        target_lang=target_lang,
        style_options=effective_style_options,
    )
    log_info(
        "[Pre-Translation Analysis] stage elapsed "
        f"{time.perf_counter() - analysis_stage_start:.2f}s"
    )
    job_id = str(
        (pre_analysis.get("job_id") if isinstance(pre_analysis, dict) else "")
        or temporary_glossary.get("job_id")
        or style_job_id
        or ""
    )
    if pre_analysis:
        initial_glossary_analysis = (
            pre_analysis.get("initial_glossary_analysis")
            if isinstance(pre_analysis.get("initial_glossary_analysis"), dict)
            else {}
        )
        if initial_glossary_analysis:
            glossary_dump_path = save_initial_glossary_to_local_file(
                job_id,
                initial_glossary_analysis,
                artifact_label=artifact_label,
            )
            pre_analysis["_initial_glossary_dump_path"] = glossary_dump_path or None
        dump_path = save_pre_analysis_to_local_file(job_id, pre_analysis, artifact_label=artifact_label)
        pre_analysis["_dump_path"] = dump_path or None
        effective_style_options = {
            **(effective_style_options or {}),
            "_pre_translation_analysis": pre_analysis,
        }
        if on_pre_translation_analysis:
            await on_pre_translation_analysis(pre_analysis)
    if term_memory is None:
        dtm_stage_start = time.perf_counter()
        term_memory = create_document_term_memory(
            pre_analysis,
            job_id=job_id,
            target_lang=target_lang,
            evidence_memory=temporary_glossary,
        )
        if term_memory:
            term_memory["_artifact_label"] = artifact_label or None
            pre_judge_stage_start = time.perf_counter()
            pre_judge_result = await run_document_term_pre_judge(
                sem,
                session,
                term_memory,
                target_lang=target_lang,
                evidence_memory=temporary_glossary,
                apply=True,
            )
            if pre_judge_result:
                log_info(
                    "[Document Term Pre-Judge] applied "
                    f"{(pre_judge_result.get('apply_result') or {})} "
                    f"elapsed={time.perf_counter() - pre_judge_stage_start:.2f}s"
                )
            term_memory_path = save_document_term_memory_to_local_file(
                job_id,
                term_memory,
                artifact_label=artifact_label,
            )
            term_memory["_dump_path"] = term_memory_path or None
            effective_style_options = {
                **(effective_style_options or {}),
                "_document_term_memory_memory": term_memory,
            }
            log_info(
                "[Document Term Memory] initialized "
                f"{document_term_memory_summary(term_memory)}"
                f"{f' dump_path={term_memory_path}' if term_memory_path else ''} "
                f"elapsed={time.perf_counter() - dtm_stage_start:.2f}s"
            )
            if on_document_term_memory_update:
                await on_document_term_memory_update(term_memory)
    if summary_memory is None:
        scopes = {
            unit.context_scope or f"unit:{unit.translation_unit_id}"
            for unit in translation_units
        }
        doc_format = "office"
        if any(scope.startswith("docx:") for scope in scopes):
            doc_format = "docx"
        elif any(scope.startswith("pptx:") for scope in scopes):
            doc_format = "pptx"
        elif any(scope.startswith("xlsx:") for scope in scopes):
            doc_format = "xlsx"
        summary_memory = create_bilingual_summary_memory(
            job_id=job_id,
            target_lang=target_lang,
            doc_format=doc_format,
            translation_units=translation_units,
            style_options=effective_style_options,
        )
        if summary_memory:
            summary_path = save_bilingual_summary_memory_to_local_file(
                job_id,
                summary_memory,
                artifact_label=artifact_label,
            )
            summary_memory["_dump_path"] = summary_path or None
            effective_style_options = {
                **(effective_style_options or {}),
                "_bilingual_summary_memory_memory": summary_memory,
            }
    log_info(
        "[Document Term Memory] setup elapsed "
        f"{time.perf_counter() - memory_stage_start:.2f}s"
    )
    return TranslationMemorySetup(
        style_options=effective_style_options,
        temporary_glossary=temporary_glossary,
        pre_translation_analysis=pre_analysis,
        document_term_memory=term_memory,
        bilingual_summary_memory=summary_memory,
    )
