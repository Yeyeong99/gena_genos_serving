"""Lightweight retrieval helpers for local translation memory."""

from __future__ import annotations

import math
import re

_SEARCH_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[가-힣]+")


def search_tokens(value: str) -> list[str]:
    return [
        token.lower()
        for token in _SEARCH_TOKEN_RE.findall(str(value or ""))
        if token.strip()
    ]


def bm25_rank_documents(
    query: str,
    documents: list[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[tuple[float, int]]:
    """Return ``(score, index)`` pairs sorted by BM25 score descending."""

    query_terms = search_tokens(query)
    if not documents or not query_terms:
        return []

    tokenized_documents = [search_tokens(document) for document in documents]
    document_count = len(tokenized_documents)
    average_document_length = sum(len(document) for document in tokenized_documents) / max(1, document_count)
    document_frequency: dict[str, int] = {}
    for document in tokenized_documents:
        for token in set(document):
            document_frequency[token] = document_frequency.get(token, 0) + 1

    query_term_set = set(query_terms)
    ranked: list[tuple[float, int]] = []
    for index, document in enumerate(tokenized_documents):
        if not document:
            continue
        term_counts: dict[str, int] = {}
        for token in document:
            if token in query_term_set:
                term_counts[token] = term_counts.get(token, 0) + 1
        if not term_counts:
            continue

        document_length = len(document)
        score = 0.0
        for token, frequency in term_counts.items():
            df = document_frequency.get(token, 0)
            idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (
                1 - b + b * document_length / max(average_document_length, 1e-9)
            )
            score += idf * (frequency * (k1 + 1)) / denominator
        if score > 0:
            ranked.append((score, index))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked
