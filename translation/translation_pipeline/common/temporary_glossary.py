"""Legacy compatibility facade for document-local term memory.

The implementation has moved behind role-specific modules:

- ``term_extractor``: source-side candidate and occurrence extraction
- ``term_memory_store``: memory schema, lookup, applied-term history, persistence
- ``term_observer``: target-side observation updates
- ``term_resolver``: promotion/demotion boundary

This module intentionally preserves the old import surface while keeping new
callers away from the legacy name.
"""

from __future__ import annotations

from translation_pipeline.common.term_extractor import scan_terms
from translation_pipeline.common.term_memory_core import normalize_source
from translation_pipeline.common.term_memory_store import (
    create_memory,
    dumps_memory,
    find_relevant_terms,
    glossary_enabled,
    load_memory_from_redis,
    loads_memory,
    memory_summary,
    record_applied_terms,
    redis_enabled,
    redis_key,
    save_memory_to_local_file,
    save_memory_to_redis,
    update_memory_from_scan,
)
from translation_pipeline.common.term_observer import record_observed_translations

__all__ = [
    "create_memory",
    "dumps_memory",
    "find_relevant_terms",
    "glossary_enabled",
    "load_memory_from_redis",
    "loads_memory",
    "memory_summary",
    "normalize_source",
    "record_applied_terms",
    "record_observed_translations",
    "redis_enabled",
    "redis_key",
    "save_memory_to_local_file",
    "save_memory_to_redis",
    "scan_terms",
    "update_memory_from_scan",
]
