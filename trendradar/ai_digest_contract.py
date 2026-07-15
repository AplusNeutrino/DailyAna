"""Strict, dependency-light contract validation for AI Digest model output."""

from __future__ import annotations

from typing import Any, Iterable


def validate_digest_analysis(parsed: dict[str, Any], expected_ids: Iterable[str] = ()) -> None:
    if not isinstance(parsed.get("items"), list) or not isinstance(parsed.get("daily"), dict):
        raise ValueError("AI response must contain items[] and daily{}")
    rows = parsed["items"]
    if not all(isinstance(row, dict) and isinstance(row.get("digest_id"), str) for row in rows):
        raise ValueError("every AI item must contain a string digest_id")
    ids = [row["digest_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("AI response contains duplicate digest_id values")
    expected = set(expected_ids)
    if expected and set(ids) != expected:
        raise ValueError(
            f"AI response digest_id mismatch: missing={sorted(expected - set(ids))} "
            f"unexpected={sorted(set(ids) - expected)}"
        )
