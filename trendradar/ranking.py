"""Deterministic Ravenis A/B/C ranking.

The model may summarize ranked evidence, but it never supplies ranking scores.
Private score components remain in run packages; public projections only expose
the total and two deterministic explanations.
"""

from __future__ import annotations

import hashlib
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

COMPONENTS = (
    "relevance",
    "source_quality",
    "evidence",
    "impact",
    "novelty",
    "recency",
    "momentum",
    "actionability",
)
DEFAULT_SLOT_PROFILES = {"A": "A", "DIGEST": "A", "B": "B", "C": "C"}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_ranking_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", _text(value)).casefold()


def _terms(value: Any) -> list[str]:
    return [normalize_ranking_text(item) for item in (value or []) if _text(item)]


def _matches(haystack: str, values: Any) -> list[str]:
    return list(dict.fromkeys(term for term in _terms(values) if term and term in haystack))


def _domain(value: Any) -> str:
    try:
        host = (urlsplit(_text(value)).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _safe_url(value: Any) -> bool:
    try:
        parts = urlsplit(_text(value))
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def _fingerprint(record: Mapping[str, Any]) -> str:
    payload = "|".join(
        (
            normalize_ranking_text(record.get("title")),
            normalize_ranking_text(record.get("summary")),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class ScoringContext:
    rules: Mapping[str, Any]
    now: datetime
    recent_records: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    history_status: str = "ok"


@dataclass(frozen=True)
class RankResult:
    score: int
    perspective: str
    components: Mapping[str, int]
    reasons: tuple[str, ...]
    penalties: tuple[str, ...] = ()

    def to_private_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "perspective": self.perspective,
            "components": dict(self.components),
            "reasons": list(self.reasons),
            "penalties": list(self.penalties),
        }

    def to_public_dict(self, record_id: str) -> dict[str, Any]:
        return {"id": record_id, "score": self.score, "reasons": list(self.reasons[:2])}


def perspective_for_slot(slot: Any, rules: Mapping[str, Any]) -> str:
    scoring = rules.get("scoring", {}) or {}
    profiles = scoring.get("slot_profiles", {}) or DEFAULT_SLOT_PROFILES
    perspective = _text(profiles.get(_text(slot).upper())).upper()
    if perspective not in {"A", "B", "C"}:
        raise ValueError(f"slot {_text(slot)!r} does not map to an A/B/C scoring perspective")
    return perspective


def _profile(context: ScoringContext, perspective: str) -> Mapping[str, Any]:
    scoring = context.rules.get("scoring", {}) or {}
    if int(scoring.get("version") or 0) != 2:
        raise ValueError("intelligence scoring.version must be 2")
    profile = (scoring.get("profiles", {}) or {}).get(perspective)
    if not isinstance(profile, Mapping):
        raise ValueError(f"missing scoring profile {perspective}")
    weights = profile.get("weights", {}) or {}
    if set(weights) != set(COMPONENTS):
        raise ValueError(f"scoring profile {perspective} must define exactly eight components")
    if abs(sum(float(weights[name]) for name in COMPONENTS) - 1.0) > 1e-9:
        raise ValueError(f"scoring profile {perspective} weights must sum to 1.0")
    return profile


def _source_entries(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = [dict(item) for item in (record.get("sources") or []) if isinstance(item, Mapping)]
    if entries:
        return entries
    return [{
        "source": record.get("source", ""),
        "source_id": record.get("source_id", ""),
        "source_type": record.get("source_type", ""),
        "publisher_key": record.get("publisher_key", ""),
        "url": record.get("url", ""),
    }]


def _source_score(entry: Mapping[str, Any], rules: Mapping[str, Any]) -> tuple[int, bool, bool]:
    quality = rules.get("source_quality", {}) or {}
    default = int(quality.get("default", 60))
    source_id = normalize_ranking_text(entry.get("source_id"))
    publisher_key = normalize_ranking_text(entry.get("publisher_key"))
    source_name = normalize_ranking_text(entry.get("source"))
    source_type = normalize_ranking_text(entry.get("source_type"))
    domain = _domain(entry.get("url"))
    exact = quality.get("sources", {}) or {}
    for key in (publisher_key, source_id, source_name):
        if key and key in exact:
            score = int(exact[key])
            social = key in {normalize_ranking_text(v) for v in quality.get("social_sources", []) or []}
            return score, True, social
    for configured_domain, raw_score in (quality.get("domains", {}) or {}).items():
        configured = normalize_ranking_text(configured_domain).removeprefix("www.")
        if domain and (domain == configured or domain.endswith("." + configured)):
            return int(raw_score), True, False
    identity = " ".join((publisher_key, source_id, source_name, domain))
    for pattern in quality.get("patterns", []) or []:
        if _matches(identity, pattern.get("contains", [])):
            return int(pattern.get("score", default)), True, False
    social_ids = {normalize_ranking_text(v) for v in quality.get("social_sources", []) or []}
    if source_id in social_ids or publisher_key in social_ids or source_type in {"social", "individual"}:
        return int(quality.get("social_score", 48)), True, True
    type_scores = quality.get("source_type_scores", {}) or {}
    if source_type in type_scores:
        return int(type_scores[source_type]), True, source_type in {"social", "individual"}
    if source_type == "rss":
        return int(quality.get("rss_default", 75)), False, False
    return default, False, False


def _source_component(record: Mapping[str, Any], rules: Mapping[str, Any]) -> tuple[int, bool, bool]:
    scored = [_source_score(entry, rules) for entry in _source_entries(record)]
    values = [max(0, min(100, value[0])) for value in scored] or [60]
    highest = max(values)
    median = statistics.median(values)
    return int(round(highest * 0.7 + median * 0.3)), any(v[1] for v in scored), any(v[2] for v in scored)


def _relevance(record: Mapping[str, Any], profile: Mapping[str, Any]) -> tuple[int, bool]:
    category_id = _text(record.get("category_id")) or "other"
    base = int((profile.get("category_base", {}) or {}).get(category_id, 40))
    haystack = normalize_ranking_text(" ".join((
        _text(record.get("title")),
        _text(record.get("summary")),
        _text(record.get("category")),
        " ".join(_text(v) for v in record.get("tags", []) or []),
    )))
    keywords = profile.get("keywords", {}) or {}
    core = _matches(haystack, keywords.get("core", []))
    secondary = _matches(haystack, keywords.get("secondary", []))
    excluded = _matches(haystack, keywords.get("exclude", []))
    value = base
    if core:
        value = max(value, 90) + min(10, max(0, len(core) - 1) * 3)
    elif secondary:
        value = max(value, 70) + min(6, max(0, len(secondary) - 1) * 2)
    if excluded:
        value = min(value, 30)
    return max(0, min(100, value)), bool(core)


def _evidence(record: Mapping[str, Any]) -> tuple[int, int]:
    entries = _source_entries(record)
    identities = {
        normalize_ranking_text(item.get("publisher_key") or item.get("source_id") or _domain(item.get("url")) or item.get("source"))
        for item in entries
    }
    identities.discard("")
    # A detailed source list is authoritative.  Only legacy/cluster records
    # without it may fall back to their already-normalized source_count.
    count = len(identities) if record.get("sources") else max(len(identities), int(record.get("source_count") or 1))
    count = max(1, count)
    value = 35 if count <= 1 else 70 if count == 2 else 85 if count == 3 else 100
    return value, count


_SPECIFIC_RE = re.compile(r"(?:\d+(?:\.\d+)?%?|\d{4}年|\d+月\d+日|q[1-4]|v?\d+\.\d+)", re.I)


def _impact(record: Mapping[str, Any], profile: Mapping[str, Any]) -> int:
    haystack = normalize_ranking_text(" ".join((_text(record.get("title")), _text(record.get("summary")))))
    terms = profile.get("impact_keywords", {}) or {}
    value = 40
    if _matches(haystack, terms.get("high", [])):
        value += 30
    elif _matches(haystack, terms.get("medium", [])):
        value += 15
    if _SPECIFIC_RE.search(haystack):
        value += 10
    if _matches(haystack, terms.get("broad", [])):
        value += 10
    return min(100, value)


def _novelty(record: Mapping[str, Any], context: ScoringContext) -> tuple[int, str]:
    if context.history_status == "degraded":
        return 50, "degraded"
    previous = context.recent_records.get(_text(record.get("id")))
    if not previous:
        # Many crawler payloads already carry their first/last occurrence even
        # when the optional 30-day lookup is unavailable.  Use that evidence so
        # a recurring headline is not promoted as brand new on every run.
        first_seen = _parse_time(record.get("first_seen"))
        last_seen = _parse_time(record.get("last_seen"))
        reference = context.now.replace(tzinfo=None)
        occurrence_count = int(record.get("occurrence_count") or 1)
        has_history = occurrence_count > 1 or (
            first_seen is not None and (reference - first_seen).total_seconds() > 24 * 3600
        )
        if has_history:
            changed = int(record.get("source_count") or 1) > 1 or (
                first_seen is not None
                and last_seen is not None
                and (last_seen - first_seen).total_seconds() > 0
                and bool(record.get("substantive_update"))
            )
            return (70, "updated") if changed else (25, "repeat")
        return 100, "new"
    source_growth = int(record.get("source_count") or 1) > int(previous.get("source_count") or 1)
    previous_fingerprint = _text(previous.get("fingerprint")) or _fingerprint(previous)
    if source_growth or previous_fingerprint != _fingerprint(record):
        return 70, "updated"
    return 25, "repeat"


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Runtime timestamps and slot boundaries are local wall-clock values.  Keep
    # an explicit offset's wall time when comparing with datetime.now(), rather
    # than mixing UTC-naive and local-naive timestamps.
    return parsed.replace(tzinfo=None)


def _recency(record: Mapping[str, Any], now: datetime) -> int:
    captured = _parse_time(record.get("published_at") or record.get("captured_at") or record.get("last_seen"))
    if captured is None:
        return 50
    reference = now.replace(tzinfo=None)
    hours = max(0.0, (reference - captured).total_seconds() / 3600)
    return 100 if hours <= 6 else 80 if hours <= 24 else 50 if hours <= 72 else 20


def _momentum(record: Mapping[str, Any], context: ScoringContext) -> int:
    if context.history_status == "degraded":
        return 40
    rank = int(record.get("platform_rank") or 0)
    value = 100 if 0 < rank <= 3 else 80 if rank <= 10 and rank else 60 if rank <= 20 and rank else 40
    previous = context.recent_records.get(_text(record.get("id"))) or {}
    previous_rank = int(previous.get("platform_rank") or 0)
    occurrence_growth = int(record.get("occurrence_count") or 1) > int(previous.get("occurrence_count") or 1)
    if (rank and previous_rank and rank < previous_rank) or occurrence_growth:
        value = min(100, value + 10)
    return value


def _actionability(record: Mapping[str, Any], profile: Mapping[str, Any]) -> int:
    haystack = normalize_ranking_text(" ".join((_text(record.get("title")), _text(record.get("summary")))))
    value = 35
    if _matches(haystack, profile.get("action_keywords", [])):
        value += 35
    if _SPECIFIC_RE.search(haystack):
        value += 15
    if _safe_url(record.get("url")):
        value += 15
    return min(100, value)


def _reason_texts(
    record: Mapping[str, Any],
    perspective: str,
    components: Mapping[str, int],
    novelty_state: str,
    source_count: int,
) -> dict[str, str]:
    category = _text(record.get("category")) or "其他"
    source = _text(record.get("source")) or "当前来源"
    return {
        "relevance": f"{perspective} 核心主题：{category}",
        "source_quality": f"高可信来源：{source}" if components["source_quality"] >= 85 else f"来源可信度较高：{source}",
        "evidence": f"{source_count} 个独立来源交叉印证" if source_count >= 2 else "来源身份已规范化",
        "impact": "包含实质政策、产业或市场变化",
        "novelty": "近 30 天首次出现" if novelty_state == "new" else "事件出现新的实质变化",
        "recency": "6 小时内更新" if components["recency"] >= 100 else "24 小时内更新",
        "momentum": "热度或排名正在上升",
        "actionability": "包含可验证的发布、价格或执行节点",
    }


def score_record(record: Mapping[str, Any], perspective: str, context: ScoringContext) -> RankResult:
    perspective = _text(perspective).upper()
    if perspective not in {"A", "B", "C"}:
        raise ValueError(f"unknown Ravenis perspective: {perspective}")
    profile = _profile(context, perspective)
    relevance, _ = _relevance(record, profile)
    source_quality, known_source, social_source = _source_component(record, context.rules)
    evidence, source_count = _evidence(record)
    novelty, novelty_state = _novelty(record, context)
    components = {
        "relevance": relevance,
        "source_quality": source_quality,
        "evidence": evidence,
        "impact": _impact(record, profile),
        "novelty": novelty,
        "recency": _recency(record, context.now),
        "momentum": _momentum(record, context),
        "actionability": _actionability(record, profile),
    }
    weights = profile.get("weights", {}) or {}
    raw = sum(components[name] * float(weights[name]) for name in COMPONENTS)
    penalties_cfg = (context.rules.get("scoring", {}) or {}).get("penalties", {}) or {}
    penalties: list[str] = []
    intents = {normalize_ranking_text(v) for v in record.get("intent", []) or []}
    haystack = normalize_ranking_text(" ".join((_text(record.get("title")), _text(record.get("summary")))))
    if "noise" in intents:
        amount = int(penalties_cfg.get("noise", 18))
        raw -= amount
        penalties.append(f"noise:-{amount}")
    if _matches(haystack, penalties_cfg.get("generic_terms", [])):
        amount = int(penalties_cfg.get("generic", 12))
        raw -= amount
        penalties.append(f"generic:-{amount}")
    cap = 100
    if source_count == 1 and _matches(haystack, penalties_cfg.get("rumor_terms", [])):
        amount = int(penalties_cfg.get("rumor", 15))
        raw -= amount
        cap = min(cap, int(penalties_cfg.get("rumor_cap", 70)))
        penalties.append(f"rumor:-{amount}")
    if source_count == 1 and social_source:
        cap = min(cap, int(penalties_cfg.get("single_social_cap", 65)))
        penalties.append(f"single_social:cap{cap}")
    if not known_source and not _safe_url(record.get("url")):
        cap = min(cap, int(penalties_cfg.get("unknown_no_url_cap", 60)))
        penalties.append(f"unknown_no_url:cap{cap}")
    score = max(0, min(cap, int(round(raw))))
    labels = _reason_texts(record, perspective, components, novelty_state, source_count)
    ranked_reasons = sorted(
        COMPONENTS,
        key=lambda name: (components[name] * float(weights[name]), components[name]),
        reverse=True,
    )
    reasons = tuple(labels[name] for name in ranked_reasons[:2])
    return RankResult(score, perspective, components, reasons, tuple(penalties))


def ranking_sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    components = record.get("score_components", {}) or {}
    last_seen = _parse_time(record.get("last_seen") or record.get("date"))
    return (
        -int(record.get("raw_item_score") or record.get("score") or 0),
        -int(components.get("source_quality") or 0),
        -int(components.get("evidence") or 0),
        -int(last_seen.timestamp() if last_seen else 0),
        _text(record.get("id")),
    )
