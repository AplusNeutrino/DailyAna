# coding=utf-8
"""Ravenis Core intelligence packaging, rendering, and R2 archiving.

This module is intentionally rule-first.  It converts the existing crawler
output into traceable raw items and lightweight event clusters without changing
the crawler or the old notification path.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:  # pragma: no cover - handled at runtime
    boto3 = None
    BotoConfig = None


DEFAULT_CATEGORY = "其他"
PUBLIC_HISTORY_FIELDS = {
    "id", "short_id", "date", "type", "title", "source", "source_count", "url", "category",
    "tags", "score", "first_seen", "last_seen", "occurrence_count", "summary",
}
TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "spm", "utm_campaign", "utm_content", "utm_medium",
    "utm_source", "utm_term",
}


class TitleIntegrityError(ValueError):
    """Raised when a complete title cannot fit within one transport payload."""


@dataclass(frozen=True)
class DigestTopItem:
    id: str
    headline: str
    event: str
    impact: str
    watch: str
    evidence_ids: Tuple[str, ...]


@dataclass(frozen=True)
class DigestWatchItem:
    text: str
    evidence_ids: Tuple[str, ...]


@dataclass(frozen=True)
class DigestSummary:
    overview: str
    top_items: Tuple[DigestTopItem, ...]
    watchlist: Tuple[DigestWatchItem, ...]
    status: str = "rules"

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["top_items"] = [dict(item) for item in payload["top_items"]]
        payload["watchlist"] = [dict(item) for item in payload["watchlist"]]
        return payload


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_title(title: str) -> str:
    title = re.sub(r"\s+", " ", _text(title)).lower()
    title = re.sub(r"[^\w\u4e00-\u9fff]+", "", title)
    return title


def normalize_display_title(title: Any) -> str:
    """Normalize presentation noise without removing or rewriting title meaning."""
    value = re.sub(r"\s+", " ", _text(title))
    marker_pattern = r"(?P<marker>(?:\[[^\]\n]{1,20}\]|【[^】\n]{1,20}】))(?:\s*(?P=marker))+"
    previous = None
    while value != previous:
        previous = value
        value = re.sub(marker_pattern, r"\g<marker>", value)
    return value


def normalize_url(url: str) -> str:
    value = _text(url)
    if not value:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return value
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def _publisher_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _text(value).lower())


def _publisher_key(source: Any, source_id: Any, url: Any, rules: Dict[str, Any]) -> str:
    """Return one configured identity for scoring, counting, and display attribution."""
    source_token = _publisher_token(source)
    source_id_token = _publisher_token(source_id)
    observed = {token for token in (source_token, source_id_token) if token}
    aliases = rules.get("publisher_aliases", {}) or {}
    for canonical, values in aliases.items():
        alias_tokens = {
            _publisher_token(value)
            for value in [canonical, *(values or [])]
            if _publisher_token(value)
        }
        if observed & alias_tokens:
            return _publisher_token(canonical)
    if source_id_token:
        return f"id:{source_id_token}"
    if source_token:
        return f"name:{source_token}"
    normalized_url = normalize_url(_text(url))
    if normalized_url:
        try:
            return f"host:{urlsplit(normalized_url).netloc.lower()}"
        except ValueError:
            pass
    return "unknown"


def content_hash(title: str, url: str) -> str:
    base = normalize_url(url) or normalize_title(title)
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:16]


def get_slot(now: datetime, slots: Optional[Dict[str, Any]] = None) -> str:
    explicit = os.environ.get("RAVENIS_SLOT", "").strip().upper()
    if explicit:
        if not re.fullmatch(r"[A-Z][A-Z0-9_-]*", explicit):
            raise ValueError(f"Invalid RAVENIS_SLOT: {explicit!r}")
        if slots and explicit not in slots:
            raise ValueError(f"RAVENIS_SLOT is not configured: {explicit}")
        return explicit
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        raise ValueError("RAVENIS_SLOT must be set explicitly in GitHub Actions")
    configured = slots or {}
    minute_of_day = now.hour * 60 + now.minute
    best_slot = ""
    best_minute = -1
    for slot, spec in configured.items():
        time_text = _text((spec or {}).get("time"))
        match = re.match(r"^(\d{1,2}):(\d{2})", time_text)
        if match:
            slot_minute = int(match.group(1)) * 60 + int(match.group(2))
            if slot_minute <= minute_of_day and slot_minute > best_minute:
                best_slot = str(slot).upper()
                best_minute = slot_minute
    if best_slot:
        return best_slot
    hour = now.hour
    if hour < 11:
        return "A"
    if hour < 17:
        return "B"
    return "C"


def _source_badge(source_name: str, source_type: str, source_id: str) -> str:
    if source_type == "rss":
        return "RSS"
    return "热" if source_type == "hotlist" else "源"


def _classify(
    title: str,
    source_name: str,
    source_type: str,
    rules: Dict[str, Any],
) -> Tuple[str, str, List[str], List[str]]:
    haystack = f"{title} {source_name}".lower()
    category_id = "other"
    category = DEFAULT_CATEGORY
    default_intents: List[str] = ["NOISE"]
    for candidate in rules.get("categories", []) or []:
        words = candidate.get("keywords", []) or []
        if words and any(_text(word).lower() in haystack for word in words):
            category_id = _text(candidate.get("id")) or "other"
            category = _text(candidate.get("name")) or DEFAULT_CATEGORY
            default_intents = list(candidate.get("default_intents", []) or [])
            break
        if candidate.get("id") == "other":
            category_id = "other"
            category = _text(candidate.get("name")) or DEFAULT_CATEGORY
            default_intents = list(candidate.get("default_intents", []) or ["NOISE"])

    tags = [
        tag
        for tag, words in (rules.get("tags", {}) or {}).items()
        if any(_text(word).lower() in haystack for word in (words or []))
    ]

    intents: List[str] = list(default_intents)
    for intent, words in (rules.get("intents", {}) or {}).items():
        if any(_text(word).lower() in haystack for word in (words or [])):
            intents.append(intent)

    return category_id, category, tags[:8], sorted(set(intents), key=intents.index)


def _source_quality(
    source_name: str,
    source_type: str,
    source_id: str,
    rules: Dict[str, Any],
) -> int:
    quality = rules.get("source_quality", {}) or {}
    text = f"{source_name} {source_id}".lower()
    exact = quality.get("sources", {}) or {}
    if source_id in exact:
        return int(exact[source_id])
    for pattern in quality.get("patterns", []) or []:
        if any(_text(hint).lower() in text for hint in pattern.get("contains", []) or []):
            return int(pattern.get("score", quality.get("default", 62)))
    if source_id in set(quality.get("social_sources", []) or []):
        return int(quality.get("social_score", 52))
    if source_type == "rss":
        return int(quality.get("rss_default", 76))
    return int(quality.get("default", 62))


def _score_item(item: Dict[str, Any], rules: Dict[str, Any], recent_ids: set[str]) -> int:
    scoring = rules.get("scoring", {}) or {}
    weights = scoring.get("weights", {}) or {}
    required = {"relevance", "source_quality", "multi_source", "novelty", "impact"}
    if set(weights) != required or abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-9:
        raise ValueError("intelligence scoring weights must define five components and sum to 1.0")
    intents = set(item.get("intent", []))
    source_quality = _source_quality(
        item.get("source", ""), item.get("source_type", ""), item.get("source_id", ""), rules
    )
    relevance = int((scoring.get("relevance", {}) or {}).get(item.get("category_id"), 35))
    rank = int(item.get("platform_rank") or 0)
    if rank:
        relevance = min(100, relevance + max(0, 10 - min(rank, 10)))
    independent_sources = max(1, int(item.get("source_count") or 1))
    multi_source = min(100, 35 + (independent_sources - 1) * 30)
    novelty = 25 if item.get("id") in recent_ids else 100
    impact = 45
    if intents & set(scoring.get("impact_intents", []) or []):
        impact += 20
    impact = min(100, impact)
    components = {
        "relevance": relevance,
        "source_quality": source_quality,
        "multi_source": multi_source,
        "novelty": novelty,
        "impact": impact,
    }
    item["score_components"] = components
    noise_penalty = int(scoring.get("noise_penalty", 0)) if "NOISE" in intents else 0
    raw = sum(components[name] * float(weights[name]) for name in required) - noise_penalty
    return max(0, min(100, int(round(raw))))


def _iter_group_titles(groups: Optional[List[Dict[str, Any]]]) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    for group in groups or []:
        for title in group.get("titles", []) or []:
            yield group, title


def _iter_standalone(standalone_data: Optional[Dict[str, Any]]) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any], str]]:
    if not standalone_data:
        return
    for group in standalone_data.get("platforms", []) or []:
        for item in group.get("items", []) or []:
            yield group, item, "hotlist"
    for group in standalone_data.get("rss_feeds", []) or []:
        for item in group.get("items", []) or []:
            yield group, item, "rss"


def _collect_raw_candidates(report_data: Dict[str, Any], rss_items: Optional[List[Dict[str, Any]]],
                            rss_new_items: Optional[List[Dict[str, Any]]],
                            standalone_data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    for group, title in _iter_group_titles(report_data.get("stats", [])):
        candidates.append({
            "title": title.get("title"),
            "url": title.get("mobile_url") or title.get("url") or "",
            "source": title.get("source_name") or title.get("source") or group.get("word") or "",
            "source_id": title.get("source_id") or title.get("platform_id") or "",
            "source_type": "hotlist",
            "platform_rank": title.get("rank") or title.get("current_rank") or 0,
            "captured_at": title.get("crawl_time") or title.get("last_time") or "",
            "summary": title.get("summary") or "",
            "raw_payload": title,
        })

    for group, title in _iter_group_titles(report_data.get("new_titles", [])):
        candidates.append({
            "title": title.get("title"),
            "url": title.get("mobile_url") or title.get("url") or "",
            "source": title.get("source_name") or title.get("source") or group.get("source_name") or "",
            "source_id": title.get("source_id") or title.get("platform_id") or "",
            "source_type": "hotlist",
            "platform_rank": title.get("rank") or 0,
            "captured_at": title.get("crawl_time") or title.get("last_time") or "",
            "summary": title.get("summary") or "",
            "raw_payload": title,
        })

    for group, title in _iter_group_titles(rss_items):
        candidates.append({
            "title": title.get("title"),
            "url": title.get("url") or title.get("mobile_url") or "",
            "source": title.get("feed_name") or title.get("source_name") or title.get("source") or group.get("word") or "",
            "source_id": title.get("feed_id") or title.get("source_id") or "",
            "source_type": "rss",
            "platform_rank": title.get("rank") or 0,
            "captured_at": title.get("published_at") or title.get("crawl_time") or "",
            "summary": title.get("summary") or "",
            "raw_payload": title,
        })

    for group, title in _iter_group_titles(rss_new_items):
        candidates.append({
            "title": title.get("title"),
            "url": title.get("url") or title.get("mobile_url") or "",
            "source": title.get("feed_name") or title.get("source_name") or title.get("source") or group.get("word") or "",
            "source_id": title.get("feed_id") or title.get("source_id") or "",
            "source_type": "rss",
            "platform_rank": title.get("rank") or 0,
            "captured_at": title.get("published_at") or title.get("crawl_time") or "",
            "summary": title.get("summary") or "",
            "raw_payload": title,
        })

    for group, item, source_type in _iter_standalone(standalone_data):
        candidates.append({
            "title": item.get("title"),
            "url": item.get("mobile_url") or item.get("url") or "",
            "source": item.get("feed_name") or item.get("source_name") or item.get("source") or group.get("name") or group.get("source_name") or "",
            "source_id": item.get("feed_id") or item.get("source_id") or item.get("platform_id") or group.get("id") or "",
            "source_type": source_type,
            "platform_rank": item.get("rank") or 0,
            "captured_at": item.get("published_at") or item.get("crawl_time") or item.get("last_time") or "",
            "summary": item.get("summary") or "",
            "raw_payload": item,
        })

    return candidates


def _dedupe_candidates(
    candidates: List[Dict[str, Any]], rules: Dict[str, Any]
) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    ordered: List[Dict[str, Any]] = []
    for item in candidates:
        title = _text(item.get("title"))
        if not title:
            continue
        url_key = normalize_url(item.get("url", ""))
        title_key = normalize_title(title)
        keys = [key for key in (f"url:{url_key}" if url_key else "", f"title:{title_key}" if title_key else "") if key]
        if not keys:
            continue
        existing = next((seen[key] for key in keys if key in seen), None)
        source_entry = {
            "source": item.get("source", ""),
            "publisher_key": _publisher_key(
                item.get("source"), item.get("source_id"), item.get("url"), rules
            ),
            "source_id": item.get("source_id", ""),
            "source_type": item.get("source_type", ""),
            "url": normalize_url(item.get("url", "")),
        }
        if existing is not None:
            source_keys = {
                _text(source.get("publisher_key"))
                for source in existing["sources"]
            }
            if source_entry["publisher_key"] not in source_keys:
                existing["sources"].append(source_entry)
            existing["occurrence_count"] += 1
            captured_at = _text(item.get("captured_at"))
            if captured_at:
                existing["first_seen"] = min(existing["first_seen"], captured_at) if existing["first_seen"] else captured_at
                existing["last_seen"] = max(existing["last_seen"], captured_at) if existing["last_seen"] else captured_at
            existing.setdefault("duplicate_sources", []).append({
                "source": item.get("source", ""),
                "source_id": item.get("source_id", ""),
                "source_type": item.get("source_type", ""),
                "url": item.get("url", ""),
                "publisher_key": source_entry["publisher_key"],
            })
            for key in keys:
                seen[key] = existing
            continue
        item["url"] = url_key or _text(item.get("url"))
        item["sources"] = [source_entry]
        item["occurrence_count"] = 1
        item["first_seen"] = _text(item.get("captured_at"))
        item["last_seen"] = _text(item.get("captured_at"))
        for key in keys:
            seen[key] = item
        ordered.append(item)
    for item in ordered:
        independent = {
            source.get("publisher_key")
            for source in item.get("sources", [])
        }
        item["source_count"] = len({value for value in independent if value}) or 1
    return ordered


def _title_bigrams(title: str) -> set[str]:
    normalized = normalize_title(title)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index:index + 2] for index in range(len(normalized) - 1)}


def _title_similarity(left: str, right: str) -> float:
    left_parts = _title_bigrams(left)
    right_parts = _title_bigrams(right)
    union = left_parts | right_parts
    return len(left_parts & right_parts) / len(union) if union else 0.0


def _source_keys(item: Dict[str, Any]) -> set[str]:
    return {
        _text(source.get("publisher_key") or source.get("source_id") or source.get("source") or source.get("url"))
        for source in item.get("sources", [])
        if _text(source.get("publisher_key") or source.get("source_id") or source.get("source") or source.get("url"))
    }


def _make_cluster_title(category: str, tags: List[str], items: List[Dict[str, Any]]) -> str:
    if tags:
        return f"{' / '.join(tags[:2])} 相关信号"
    if category and category != "其他":
        return f"{category} 相关信号"
    return _text(items[0].get("title"))[:34] or "未命名事件"


def _build_clusters(
    items: List[Dict[str, Any]],
    date_text: str,
    slot: str,
    now: datetime,
    thresholds: Dict[str, Any],
    rules: Dict[str, Any],
) -> List[Dict[str, Any]]:
    cluster_rules = rules.get("clustering", {}) or {}
    similarity_threshold = float(cluster_rules.get("title_bigram_similarity", 0.55))
    generic_tags = set(cluster_rules.get("generic_tags", []) or [])
    min_sources = int(cluster_rules.get("min_distinct_sources", 2))
    candidates = [item for item in items if item.get("raw_item_score", 0) >= int(thresholds.get("list", 30))]
    adjacency: Dict[int, set[int]] = defaultdict(set)
    for left_index, left in enumerate(candidates):
        for right_index in range(left_index + 1, len(candidates)):
            right = candidates[right_index]
            if left.get("category_id") != right.get("category_id"):
                continue
            shared_tags = (set(left.get("tags", [])) & set(right.get("tags", []))) - generic_tags
            if not shared_tags:
                continue
            if _title_similarity(left.get("title", ""), right.get("title", "")) < similarity_threshold:
                continue
            if len(_source_keys(left) | _source_keys(right)) < min_sources:
                continue
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)

    groups: List[List[Dict[str, Any]]] = []
    visited: set[int] = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        stack = [start]
        component = []
        while stack:
            index = stack.pop()
            if index in visited:
                continue
            visited.add(index)
            component.append(candidates[index])
            stack.extend(adjacency[index] - visited)
        if len(component) >= 2 and len(set().union(*(_source_keys(item) for item in component))) >= min_sources:
            groups.append(component)

    clusters: List[Dict[str, Any]] = []
    for seq, group in enumerate(sorted(groups, key=lambda value: max(x.get("raw_item_score", 0) for x in value), reverse=True), 1):
        tags: List[str] = []
        for item in group:
            for tag in item.get("tags", []):
                if tag not in tags:
                    tags.append(tag)
        sources = set().union(*(_source_keys(item) for item in group))
        base_score = max(item.get("raw_item_score", 0) for item in group)
        cluster_score = min(100, base_score + min(15, max(0, len(sources) - 1) * 5))
        member_ids = sorted(item["id"] for item in group)
        cluster_id = "c_" + hashlib.sha1("\n".join(member_ids).encode("utf-8")).hexdigest()[:16]
        related = [item["id"] for item in group[:6]]
        related_short = [item["short_id"] for item in group[:6]]
        summary = _cluster_summary(group)
        cluster = {
            "cluster_id": cluster_id,
            "short_cluster_id": f"{slot}C{seq:02d}",
            "date": date_text,
            "slot": slot,
            "title": _make_cluster_title(group[0].get("category", "其他"), tags, group),
            "related_item_ids": related,
            "related_short_ids": related_short,
            "category": group[0].get("category", "其他"),
            "tags": tags[:10],
            "cluster_score": cluster_score,
            "source_count": len(sources),
            "signal_strength": "高" if cluster_score >= 80 else "中高" if cluster_score >= 65 else "中",
            "summary": summary,
            "observation": _cluster_observation(group[0].get("category", "其他"), tags),
            "created_at": now.isoformat(timespec="seconds"),
        }
        for item in group:
            item["cluster_id"] = cluster_id
        clusters.append(cluster)
    return clusters


def _cluster_summary(items: List[Dict[str, Any]]) -> str:
    category = items[0].get("category", "其他")
    if category == "AI / 模型":
        return "AI 相关讨论集中在模型能力、产品落地、开发者生态和部署成本。"
    if category == "芯片 / 算力":
        return "算力链条信号集中在芯片供给、成本变化、基础设施和市场预期。"
    if category == "自动驾驶 / 机器人":
        return "硬件智能化相关消息增多，重点从技术展示转向量产、订单、责任和监管。"
    if category == "宏观 / 财经 / 地缘":
        return "宏观与市场消息可能影响风险偏好，需要和科技资产走势一起观察。"
    return f"{category} 出现多源或高分信号，值得保留跟踪。"


def _cluster_observation(category: str, tags: List[str]) -> str:
    if category in {"AI / 模型", "芯片 / 算力", "开发者 / 工具 / 开源"}:
        return "后续看是否出现产品发布、采购、融资、价格变化或开发者迁移。"
    if category == "自动驾驶 / 机器人":
        return "后续看是否出现订单、量产、事故责任、监管细则或价格战。"
    if category == "宏观 / 财经 / 地缘":
        return "后续看是否持续影响科技、芯片、新能源等风险资产表现。"
    if category == "娱乐 / 体育 / 游戏":
        return "仅在跨平台共振或涉及公司商业变化时提高权重。"
    return "后续看是否有权威来源、连续报道或实质性进展。"


def build_intelligence_package(
    *,
    report_data: Dict[str, Any],
    rss_items: Optional[List[Dict[str, Any]]] = None,
    rss_new_items: Optional[List[Dict[str, Any]]] = None,
    standalone_data: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = config or {}
    rules = cfg.get("rules") or {}
    if not rules:
        raise ValueError("intelligence_rules.yaml was not loaded")
    now = now or datetime.now()
    date_text = now.date().isoformat()
    date_compact = now.strftime("%Y%m%d")
    slot = get_slot(now, cfg.get("slots"))
    thresholds = (cfg.get("scoring") or {}).get("thresholds") or {"altar": 80, "brief": 60, "list": 30}
    recent_ids = set(cfg.get("recent_content_ids", []) or [])

    candidates = _collect_raw_candidates(
        report_data, rss_items, rss_new_items, standalone_data
    )
    raw_candidates = _dedupe_candidates(candidates, rules)
    raw_items: List[Dict[str, Any]] = []
    for seq, candidate in enumerate(raw_candidates, 1):
        title = normalize_display_title(candidate.get("title"))
        source = _text(candidate.get("source"))
        source_type = _text(candidate.get("source_type")) or "hotlist"
        source_id = _text(candidate.get("source_id"))
        category_id, category, tags, intent = _classify(title, source, source_type, rules)
        stable_id = "r_" + content_hash(title, _text(candidate.get("url")))
        captured_at = _text(candidate.get("captured_at")) or now.isoformat(timespec="seconds")
        item = {
            "id": stable_id,
            "short_id": f"{slot}{seq:03d}",
            "date": date_text,
            "slot": slot,
            "seq": seq,
            "title": title,
            "normalized_title": normalize_title(title),
            "url": normalize_url(candidate.get("url", "")),
            "source": source,
            "source_id": source_id,
            "source_type": source_type,
            "source_badge": _source_badge(source, source_type, source_id),
            "platform_rank": int(candidate.get("platform_rank") or 0),
            "captured_at": captured_at,
            "first_seen": candidate.get("first_seen") or captured_at,
            "last_seen": candidate.get("last_seen") or captured_at,
            "occurrence_count": int(candidate.get("occurrence_count") or 1),
            "source_count": int(candidate.get("source_count") or 1),
            "sources": candidate.get("sources", []),
            "category_id": category_id,
            "category": category,
            "tags": tags,
            "intent": intent,
            "cluster_id": "",
            "content_hash": stable_id.removeprefix("r_"),
            "summary": _text(candidate.get("summary")),
            "duplicate_sources": candidate.get("duplicate_sources", []),
            "raw_payload": candidate.get("raw_payload", {}),
        }
        item["raw_item_score"] = _score_item(item, rules, recent_ids)
        raw_items.append(item)

    clusters = _build_clusters(raw_items, date_text, slot, now, thresholds, rules)
    category_order = [
        _text(item.get("name"))
        for item in rules.get("categories", []) or []
        if _text(item.get("name"))
    ]
    return {
        "date": date_text,
        "date_compact": date_compact,
        "slot": slot,
        "generated_at": now.isoformat(timespec="seconds"),
        "thresholds": thresholds,
        "category_order": category_order,
        "run_status": "ok",
        "candidate_count": len(candidates),
        "deduplicated_count": len(raw_candidates),
        "raw_items": raw_items,
        "clusters": clusters,
    }


def _clip(text: str, limit: int = 88) -> str:
    text = re.sub(r"\s+", " ", _text(text))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def select_digest_candidates(package: Dict[str, Any], max_candidates: int = 12) -> List[Dict[str, Any]]:
    """Select the bounded, rule-ranked evidence set shown to the summary model."""
    limit = max(1, min(int(max_candidates or 12), 12))
    return sorted(
        package.get("raw_items", []) or [],
        key=lambda item: (
            int(item.get("raw_item_score") or 0),
            int(item.get("source_count") or 1),
            int(item.get("occurrence_count") or 1),
            _text(item.get("last_seen")),
        ),
        reverse=True,
    )[:limit]


def _select_top_items(candidates: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    per_category: Dict[str, int] = defaultdict(int)
    categories = {_text(item.get("category")) for item in candidates if _text(item.get("category"))}
    for item in candidates:
        category = _text(item.get("category")) or DEFAULT_CATEGORY
        if per_category[category] >= 2:
            continue
        if len(selected) == 1 and len(categories) > 1 and category == _text(selected[0].get("category")):
            continue
        selected.append(item)
        per_category[category] += 1
        if len(selected) >= limit:
            return selected
    for item in candidates:
        if item in selected:
            continue
        category = _text(item.get("category")) or DEFAULT_CATEGORY
        if per_category[category] >= 2:
            continue
        selected.append(item)
        per_category[category] += 1
        if len(selected) >= limit:
            break
    return selected


def _rule_watch(item: Dict[str, Any]) -> str:
    category = _text(item.get("category"))
    rules = {
        "AI / 模型": "观察是否出现产品发布、调用价格、客户采用或开发者迁移。",
        "芯片 / 算力": "观察是否出现供货、价格、订单、产能或监管数据。",
        "自动驾驶 / 机器人": "观察是否出现订单、量产、事故责任或监管细则。",
        "宏观 / 财经 / 地缘": "观察是否继续传导至利率、汇率或科技资产价格。",
        "开发者 / 工具 / 开源": "观察是否出现版本发布、真实迁移或商业化数据。",
    }
    if category in rules:
        return _clip(rules[category], 40)
    return "观察是否出现第二来源、正式数据或明确后续进展。"


def _rule_evidence(item: Dict[str, Any]) -> str:
    source_count = max(1, int(item.get("source_count") or 1))
    score = int(item.get("raw_item_score") or 0)
    novelty = int((item.get("score_components") or {}).get("novelty") or 0)
    occurrence_count = max(1, int(item.get("occurrence_count") or 1))
    parts = ["本时段首次出现" if novelty >= 100 else f"累计出现 {occurrence_count} 次"]
    if source_count >= 2:
        parts.append(f"{source_count} 个独立发布者")
    else:
        parts.append("单一发布者")
    if score:
        parts.append(f"综合评分 {score}")
    return " · ".join(parts)


def build_rule_digest_summary(
    package: Dict[str, Any], config: Optional[Dict[str, Any]] = None
) -> DigestSummary:
    cfg = config or {}
    wechat = cfg.get("wechat") or {}
    summary_cfg = cfg.get("summary") or {}
    max_candidates = int(summary_cfg.get("max_candidates", 12))
    max_top = max(1, min(int(wechat.get("max_top_items", 3)), 3))
    max_watch = max(0, min(int(wechat.get("max_watch_items", 2)), 2))
    candidates = select_digest_candidates(package, max_candidates)
    selected = _select_top_items(candidates, max_top)
    top_items = tuple(
        DigestTopItem(
            id=_text(item.get("id")),
            headline=normalize_display_title(item.get("title", "")),
            event="",
            impact=_rule_evidence(item),
            watch=_rule_watch(item),
            evidence_ids=(_text(item.get("id")),),
        )
        for item in selected
        if _text(item.get("id")) and _text(item.get("title"))
    )
    selected_ids = {item.id for item in top_items}
    watch_items: List[DigestWatchItem] = []
    seen_watch_text: set[str] = set()
    for item in candidates:
        item_id = _text(item.get("id"))
        if not item_id or item_id in selected_ids:
            continue
        text = _rule_watch(item)
        if text in seen_watch_text:
            continue
        seen_watch_text.add(text)
        watch_items.append(DigestWatchItem(text=text, evidence_ids=(item_id,)))
        if len(watch_items) >= max_watch:
            break
    categories = list(dict.fromkeys(_text(item.get("category")) for item in selected if _text(item.get("category"))))
    if top_items:
        focus = "、".join(categories[:3]) or "高分条目"
        overview = _clip(f"本时段重点集中在{focus}。", 80)
    else:
        overview = "本时段无新增强信号。"
    return DigestSummary(overview, top_items, tuple(watch_items), status="rules")


_GENERIC_SUMMARY_VALUES = {
    "值得关注", "值得观察", "持续观察", "可能影响", "后续观察", "需持续观察",
    "该事件值得关注", "该事件值得观察", "暂无", "无",
}


def _validated_digest_text(value: Any, limit: int, field: str) -> str:
    text = _clip(value, limit)
    compact = re.sub(r"[\s，。；：、,.!?！？]", "", text)
    if not text:
        raise ValueError(f"digest {field} is empty")
    if compact in {re.sub(r"[\s，。；：、,.!?！？]", "", value) for value in _GENERIC_SUMMARY_VALUES}:
        raise ValueError(f"digest {field} is generic")
    return text


def _validate_digest_source_claim(text: str, source_count: int) -> None:
    claimed_counts = {
        int(value)
        for value in re.findall(r"(\d+)\s*个(?:独立)?(?:来源|发布者)", text)
    }
    if claimed_counts and claimed_counts != {source_count}:
        raise ValueError("digest source count contradicts canonical publishers")
    if source_count == 1 and re.search(r"多源|多个(?:独立)?(?:来源|发布者)", text):
        raise ValueError("digest claims multiple publishers for a single-publisher item")
    if source_count > 1 and re.search(r"单一(?:来源|发布者)", text):
        raise ValueError("digest claims a single publisher for multi-publisher evidence")


def validate_digest_summary(
    raw_summary: Any,
    package: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> DigestSummary:
    """Fail closed when model output is untraceable, generic, or exceeds the public contract."""
    cfg = config or {}
    wechat = cfg.get("wechat") or {}
    summary_cfg = cfg.get("summary") or {}
    candidates = select_digest_candidates(package, int(summary_cfg.get("max_candidates", 12)))
    allowed_ids = {_text(item.get("id")) for item in candidates}
    payload = raw_summary.get("digest_summary") if isinstance(raw_summary, dict) and "digest_summary" in raw_summary else raw_summary
    if not isinstance(payload, dict):
        raise ValueError("digest summary must be an object")
    if set(payload) - {"overview", "top_items", "watchlist"}:
        raise ValueError("digest summary contains unknown fields")
    overview = _validated_digest_text(payload.get("overview"), 80, "overview")
    raw_top = payload.get("top_items")
    if not isinstance(raw_top, list) or not raw_top:
        raise ValueError("digest top_items must be a non-empty list")
    max_top = max(1, min(int(wechat.get("max_top_items", 3)), 3))
    top_items: List[DigestTopItem] = []
    seen_ids: set[str] = set()
    allowed_top_fields = {"id", "headline", "event", "impact", "watch", "evidence_ids"}
    for raw_item in raw_top[:max_top]:
        if not isinstance(raw_item, dict) or set(raw_item) - allowed_top_fields:
            raise ValueError("digest top item has an invalid shape")
        item_id = _text(raw_item.get("id"))
        if item_id not in allowed_ids or item_id in seen_ids:
            raise ValueError(f"digest references unknown or duplicate id: {item_id}")
        raw_evidence = raw_item.get("evidence_ids") or []
        if isinstance(raw_evidence, str):
            raw_evidence = [raw_evidence]
        evidence = tuple(
            dict.fromkeys(
                [item_id, *(_text(value) for value in raw_evidence if _text(value))]
            )
        )
        if any(value not in allowed_ids for value in evidence):
            raise ValueError(f"digest evidence ids are invalid for {item_id}")
        impact = _validated_digest_text(raw_item.get("impact"), 56, "impact")
        source_count = len(_digest_item_sources(evidence, package)) or 1
        _validate_digest_source_claim(impact, source_count)
        top_items.append(DigestTopItem(
            id=item_id,
            headline=_validated_digest_text(raw_item.get("headline"), 24, "headline"),
            event=_validated_digest_text(raw_item.get("event"), 56, "event"),
            impact=impact,
            watch=_validated_digest_text(raw_item.get("watch"), 40, "watch"),
            evidence_ids=evidence,
        ))
        seen_ids.add(item_id)
    raw_watch = payload.get("watchlist") or []
    if not isinstance(raw_watch, list):
        raise ValueError("digest watchlist must be a list")
    max_watch = max(0, min(int(wechat.get("max_watch_items", 2)), 2))
    watchlist: List[DigestWatchItem] = []
    for raw_item in raw_watch[:max_watch]:
        if not isinstance(raw_item, dict) or set(raw_item) - {"text", "evidence_ids"}:
            raise ValueError("digest watch item has an invalid shape")
        raw_evidence = raw_item.get("evidence_ids") or []
        if isinstance(raw_evidence, str):
            raw_evidence = [raw_evidence]
        evidence = tuple(
            dict.fromkeys(_text(value) for value in raw_evidence if _text(value))
        )
        if not evidence or any(value not in allowed_ids for value in evidence):
            raise ValueError("digest watch item references unknown evidence")
        watchlist.append(DigestWatchItem(
            text=_validated_digest_text(raw_item.get("text"), 40, "watchlist"),
            evidence_ids=evidence,
        ))
    return DigestSummary(overview, tuple(top_items), tuple(watchlist), status="ai")


def build_digest_summary(
    package: Dict[str, Any],
    raw_summary: Any = None,
    config: Optional[Dict[str, Any]] = None,
) -> DigestSummary:
    if raw_summary:
        try:
            return validate_digest_summary(raw_summary, package, config)
        except (TypeError, ValueError) as exc:
            print(f"[intelligence] AI digest rejected, using traceable rule summary: {exc}")
    return build_rule_digest_summary(package, config)


def _category_groups(
    items: List[Dict[str, Any]],
    category_order: List[str],
    min_score: int,
    max_per_category: int,
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item.get("raw_item_score", 0) >= min_score:
            grouped[item.get("category", "其他")].append(item)
    ordered = []
    for category in category_order:
        group = grouped.get(category)
        if group:
            ordered.append((category, sorted(group, key=lambda x: x.get("raw_item_score", 0), reverse=True)[:max_per_category]))
    return ordered


def _split_lines(text: str, max_bytes: int) -> List[str]:
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    batches: List[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}{line}\n"
        if current and len(candidate.encode("utf-8")) > max_bytes:
            batches.append(current.rstrip())
            current = f"{line}\n"
        else:
            current = candidate
    if current.strip():
        batches.append(current.rstrip())
    return batches


def _render_legacy_wechat_intelligence_messages(package: Dict[str, Any], config: Optional[Dict[str, Any]] = None,
                                                max_bytes: int = 4000) -> List[str]:
    cfg = config or {}
    wechat = cfg.get("wechat") or {}
    thresholds = package.get("thresholds") or {}
    altar_threshold = int(thresholds.get("altar", 80))
    brief_threshold = int(thresholds.get("brief", 60))
    list_threshold = int(thresholds.get("list", 30))
    max_clusters_altar = int(wechat.get("max_clusters_in_altar", 5))
    max_clusters_trial = int(wechat.get("max_clusters_in_trial", 5))
    max_items_per_category = int(wechat.get("max_items_per_category", 12))
    max_observations = int(wechat.get("max_observations", 6))

    raw_items = package.get("raw_items", [])
    clusters = sorted(package.get("clusters", []), key=lambda c: c.get("cluster_score", 0), reverse=True)
    high_clusters = [c for c in clusters if c.get("cluster_score", 0) >= altar_threshold][:max_clusters_altar]
    trial_clusters = clusters[:max_clusters_trial]
    generated_at = package.get("generated_at", "")
    display_time = generated_at[:16].replace("T", " ") if generated_at else package.get("date", "")
    slot = str(package.get("slot") or "").upper()
    slot_spec = (cfg.get("slots") or {}).get(slot, {}) or {}
    slot_parts = [slot]
    if slot_spec.get("label"):
        slot_parts.append(_text(slot_spec.get("label")))
    if slot_spec.get("time"):
        slot_parts.append(_text(slot_spec.get("time")))
    slot_display = "/".join(part for part in slot_parts if part)
    header = f"Ravenis Core | Normai Tracking Details\n{display_time} | {slot_display} | 智能新闻速递"

    msg0 = [header, "", "0. 量子祈坛", "", "今日高权重事件："]
    if not high_clusters:
        msg0.append("暂无达到高权重阈值的事件簇。")
    for cluster in high_clusters:
        msg0.extend([
            "",
            f"{cluster['short_cluster_id']}｜{_clip(cluster['title'], 42)}",
            f"涉及：{' / '.join(cluster.get('related_short_ids', [])[:6])}",
            f"判断：{_clip(cluster.get('summary', ''), 96)}",
            f"观察：{_clip(cluster.get('observation', ''), 96)}",
        ])

    msg1 = ["1. 量子知识之塔", "", "1.1 情报速报", ""]
    briefing = _briefing_lines(raw_items, clusters, brief_threshold)[:4]
    if briefing:
        msg1.extend(f"{idx}. {line}" for idx, line in enumerate(briefing, 1))
    else:
        msg1.append("1. 暂无显著情报速报。")
    msg1.extend(["", "1.2 全部信息列表"])
    category_order = package.get("category_order") or sorted({item.get("category", DEFAULT_CATEGORY) for item in raw_items})
    for category, group in _category_groups(raw_items, category_order, list_threshold, max_items_per_category):
        msg1.extend(["", f"【{category}】{len(group)} 条"])
        for item in group:
            msg1.append(f"- {item['short_id']} [{item['source_badge']}] {_clip(item['title'], 58)}")

    msg2 = ["2. 量子试炼之塔", "", "2.1 多源整合摘要"]
    if not trial_clusters:
        msg2.append("暂无可聚合事件簇，已保留单条新闻入库。")
    for cluster in trial_clusters:
        msg2.extend([
            "",
            f"{cluster['short_cluster_id']}｜{_clip(cluster['title'], 42)}",
            f"关联：{' / '.join(cluster.get('related_short_ids', [])[:6])}",
            f"摘要：{_clip(cluster.get('summary', ''), 100)}",
            f"信号：{cluster.get('signal_strength', '中')}",
            f"观察：{_clip(cluster.get('observation', ''), 100)}",
        ])
    observations = _observation_lines(raw_items, clusters)[:max_observations]
    if observations:
        msg2.extend(["", "2.2 后续观察"])
        msg2.extend(f"- {line}" for line in observations)

    messages: List[str] = []
    for text in ("\n".join(msg0), "\n".join(msg1), "\n".join(msg2)):
        messages.extend(_split_lines(text.strip(), max_bytes))
    return [message for message in messages if message.strip()]


def _digest_item_sources(item_ids: Iterable[str], package: Dict[str, Any]) -> List[str]:
    item_by_id = {_text(item.get("id")): item for item in package.get("raw_items", []) or []}
    names: List[str] = []
    seen_keys: set[str] = set()
    for item_id in item_ids:
        item = item_by_id.get(_text(item_id)) or {}
        for source in item.get("sources", []) or []:
            name = _text(source.get("source") or source.get("source_name") or source.get("source_id"))
            key = _text(
                source.get("publisher_key")
                or source.get("source_id")
                or source.get("source")
                or source.get("url")
            )
            if name and key not in seen_keys:
                seen_keys.add(key)
                names.append(name)
        name = _text(item.get("source"))
        fallback_key = _text(item.get("source_id") or name)
        if not item.get("sources") and name and fallback_key not in seen_keys:
            seen_keys.add(fallback_key)
            names.append(name)
    return names


def _history_detail_url(base_url: str, date: str, slot: str) -> str:
    base = _text(base_url)
    if not base:
        return ""
    try:
        parts = urlsplit(base)
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"date": date, "slot": slot})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _editorial_category_lines(
    package: Dict[str, Any],
    selected_ids: set[str],
    total_budget: int,
    max_per_category: int,
    *,
    markdown: bool,
) -> List[str]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    list_threshold = int((package.get("thresholds") or {}).get("list", 30))
    for item in sorted(
        package.get("raw_items", []) or [],
        key=lambda value: int(value.get("raw_item_score") or 0),
        reverse=True,
    ):
        item_id = _text(item.get("id"))
        if item_id in selected_ids or int(item.get("raw_item_score") or 0) < list_threshold:
            continue
        grouped[_text(item.get("category")) or DEFAULT_CATEGORY].append(item)
    ordered = sorted(
        grouped.items(),
        key=lambda pair: int(pair[1][0].get("raw_item_score") or 0),
        reverse=True,
    )
    lines: List[str] = []
    remaining = max(0, total_budget)
    for category, items in ordered:
        chosen = items[: min(max_per_category, remaining)]
        if not chosen:
            continue
        category_label = f"**{category}**" if markdown else category
        lines.extend(["", category_label])
        lines.extend(
            f"• {normalize_display_title(item.get('title', ''))}"
            for item in chosen
        )
        remaining -= len(chosen)
        if remaining <= 0:
            break
    return lines


def _format_digest_date(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return f"{parsed.month}月{parsed.day}日"
    except (TypeError, ValueError):
        return value


def _event_adds_title_information(event: Any, title: Any) -> bool:
    event_text = _text(event)
    title_text = normalize_display_title(title)
    if not event_text:
        return False
    event_key = normalize_title(event_text)
    title_key = normalize_title(title_text)
    if not event_key or not title_key:
        return False
    if event_key in title_key or title_key in event_key:
        return False
    return _title_similarity(event_text, title_text) < 0.72


def _split_editorial_blocks(text: str, max_bytes: int) -> List[str]:
    """Split at editorial block boundaries; never slice a title or paragraph."""
    source_blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    blocks: List[str] = []
    for block in source_blocks:
        if len(block.encode("utf-8")) <= max_bytes:
            blocks.append(block)
            continue
        first_line = block.splitlines()[0].strip()
        if re.match(r"(?:\*\*)?\d+\.\s", first_line):
            raise TitleIntegrityError(
                "one complete top item exceeds the WeCom hard limit; refusing to truncate its title"
            )
        for line in (value.strip() for value in block.splitlines() if value.strip()):
            if len(line.encode("utf-8")) > max_bytes:
                raise TitleIntegrityError(
                    "one complete title exceeds the WeCom hard limit; refusing to truncate it"
                )
            blocks.append(line)
    messages: List[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate.encode("utf-8")) > max_bytes:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    if any(len(message.encode("utf-8")) > max_bytes for message in messages):
        raise AssertionError("editorial boundary splitter exceeded hard byte limit")
    return messages


def render_wechat_intelligence_messages(
    package: Dict[str, Any],
    digest_summary: Optional[Any] = None,
    config: Optional[Dict[str, Any]] = None,
    max_bytes: int = 4000,
    output_format: str = "markdown",
) -> List[str]:
    """Render a title-safe mobile brief; 4000 bytes remains a hard transport limit."""
    if config is None and isinstance(digest_summary, dict) and (
        "wechat" in digest_summary or "slots" in digest_summary or "rules" in digest_summary
    ) and "top_items" not in digest_summary:
        config = digest_summary
        digest_summary = None
    cfg = config or {}
    wechat = cfg.get("wechat") or {}
    layout = _text(cfg.get("layout") or wechat.get("layout") or "editorial_v2")
    if layout != "editorial_v2":
        return _render_legacy_wechat_intelligence_messages(package, cfg, max_bytes=max_bytes)

    markdown = _text(output_format).lower() != "text"

    def heading(text: str, level: int) -> str:
        return f"{'#' * level} {text}" if markdown else text

    def emphasized(text: str) -> str:
        return f"**{text}**" if markdown else text

    if isinstance(digest_summary, DigestSummary):
        summary = digest_summary
    else:
        summary = build_digest_summary(package, digest_summary, cfg)
    summary_payload = summary.to_dict()
    package["digest_summary"] = summary_payload

    slot = _text(package.get("slot")).upper()
    slot_spec = (cfg.get("slots") or {}).get(slot, {}) or {}
    slot_label = _text(slot_spec.get("label")) or {"A": "早间", "B": "午间", "C": "晚间"}.get(slot, slot)
    raw_items = package.get("raw_items", []) or []
    item_by_id = {_text(item.get("id")): item for item in raw_items}
    top_items = [
        item for item in summary_payload.get("top_items", []) or []
        if _text(item.get("id")) in item_by_id
    ]
    selected_ids = {
        _text(evidence_id)
        for item in top_items
        for evidence_id in (item.get("evidence_ids") or [item.get("id")])
        if _text(evidence_id)
    }
    detail_url = ""
    if bool(wechat.get("detail_link_enabled", False)):
        detail_url = _history_detail_url(
            wechat.get("detail_url", "https://neutriverse.uk/ravenis/"),
            _text(package.get("date")),
            slot,
        )
    max_category_items = max(0, min(int(wechat.get("category_item_budget", 6)), 6))
    per_category = max(1, min(int(wechat.get("max_items_per_category", 2)), 2))
    max_watch_items = max(0, min(int(wechat.get("max_watch_items", 2)), 2))
    target_bytes = min(max_bytes, max(1200, int(wechat.get("target_bytes", 3200))))

    def compose(
        category_budget: int,
        watch_budget: int,
        *,
        include_overview: bool = True,
        include_rule_evidence: bool = True,
    ) -> str:
        lines = [
            heading(f"Ravenis {slot_label}简报 · {_format_digest_date(_text(package.get('date')))}", 1),
            (
                f"> {len(raw_items)} 条入库 · {len(top_items)} 项重点"
                if markdown
                else f"{len(raw_items)} 条入库 · {len(top_items)} 项重点"
            ),
        ]
        overview = _text(summary_payload.get("overview"))
        if include_overview and overview:
            lines.extend(["", overview])
        if top_items:
            lines.extend(["", heading(f"先看这 {len(top_items)} 件事", 2)])
            for index, item in enumerate(top_items, 1):
                evidence_ids = item.get("evidence_ids") or [item.get("id")]
                source_record = item_by_id[_text(item.get("id"))]
                display_title = normalize_display_title(source_record.get("title"))
                sources = _digest_item_sources(evidence_ids, package)
                source_text = "、".join(sources[:3]) or "来源待核验"
                source_suffix = (
                    f" · {len(sources)} 个独立发布者"
                    if len(sources) > 1
                    else " · 单一发布者"
                )
                lines.extend(["", emphasized(f"{index}. {display_title}")])
                event = _text(item.get("event"))
                if summary.status == "ai" and _event_adds_title_information(event, display_title):
                    lines.append(f"发生：{_clip(event, 56)}")
                impact = _text(item.get("impact"))
                if summary.status == "ai" and impact:
                    lines.append(f"影响：{_clip(impact, 56)}")
                elif include_rule_evidence and impact:
                    lines.append(f"依据：{_clip(impact, 56)}")
                watch = _text(item.get("watch"))
                if watch:
                    lines.append(f"观察：{_clip(watch, 40)}")
                lines.append(f"来源：{_clip(source_text, 30)}{source_suffix}")
        else:
            lines.extend(["", "本时段无新增强信号。"])

        category_lines = _editorial_category_lines(
            package,
            selected_ids,
            category_budget,
            per_category,
            markdown=markdown,
        )
        if category_lines:
            lines.extend(["", heading("更多标题", 2), *category_lines])

        top_watch = {_text(item.get("watch")) for item in top_items}
        watchlist = [
            item for item in summary_payload.get("watchlist", []) or []
            if _text(item.get("text")) and _text(item.get("text")) not in top_watch
        ][:watch_budget]
        if watchlist:
            lines.extend(["", heading("继续观察", 2), ""])
            lines.extend(f"• {_clip(item.get('text', ''), 40)}" for item in watchlist)

        if detail_url:
            lines.append("")
            if markdown:
                lines.append(f"[查看完整日报（{len(raw_items)} 条）]({detail_url}) →")
            else:
                lines.append(f"查看完整日报（{len(raw_items)} 条）：{detail_url}")
        if summary.status != "ai":
            lines.extend(["", "`规则摘要`" if markdown else "规则摘要"])
        return "\n".join(lines).strip()

    category_budget = max_category_items
    watch_budget = max_watch_items
    message = compose(category_budget, watch_budget)
    while len(message.encode("utf-8")) > target_bytes and watch_budget > 0:
        watch_budget -= 1
        message = compose(category_budget, watch_budget)
    while len(message.encode("utf-8")) > target_bytes and category_budget > 3:
        category_budget -= 1
        message = compose(category_budget, watch_budget)
    include_rule_evidence = True
    if len(message.encode("utf-8")) > target_bytes and summary.status != "ai":
        include_rule_evidence = False
        message = compose(
            category_budget,
            watch_budget,
            include_rule_evidence=include_rule_evidence,
        )
    if len(message.encode("utf-8")) > target_bytes:
        message = compose(
            category_budget,
            watch_budget,
            include_overview=False,
            include_rule_evidence=include_rule_evidence,
        )
    if len(message.encode("utf-8")) <= max_bytes:
        return [message]
    return _split_editorial_blocks(message, max_bytes)


def _briefing_lines(items: List[Dict[str, Any]], clusters: List[Dict[str, Any]], brief_threshold: int) -> List[str]:
    categories = {item.get("category") for item in items if item.get("raw_item_score", 0) >= brief_threshold}
    lines: List[str] = []
    if {"AI / 模型", "芯片 / 算力"} & categories:
        lines.append("AI / 芯片 / 国产模型仍是科技主线，重点集中在算力、模型生态和落地成本。")
    if "自动驾驶 / 机器人" in categories:
        lines.append("自动驾驶与机器人相关消息增多，硬件智能化进入持续观察区。")
    if "宏观 / 财经 / 地缘" in categories:
        lines.append("海外宏观与地缘消息可能扰动市场情绪，需要和科技资产走势一起看。")
    if "娱乐 / 体育 / 游戏" in categories:
        lines.append("娱乐、体育、游戏信息较分散，除跨平台共振外暂不作为重点观察。")
    if not lines and clusters:
        lines.append("本轮主要信号来自少数事件簇，建议优先看高分条目和后续观察。")
    return lines


def _observation_lines(items: List[Dict[str, Any]], clusters: List[Dict[str, Any]]) -> List[str]:
    categories = {item.get("category") for item in items}
    lines: List[str] = []
    if "AI / 模型" in categories:
        lines.append("国产 AI 模型是否出现明确产品化进展，而不只是测评或社区讨论。")
    if "芯片 / 算力" in categories:
        lines.append("存储芯片、GPU、液冷等成本变化是否传导到服务器和 AI 硬件。")
    if "自动驾驶 / 机器人" in categories:
        lines.append("机器人和自动驾驶公司是否出现订单、融资、量产、事故责任或监管细则。")
    if "宏观 / 财经 / 地缘" in categories:
        lines.append("宏观消息是否持续影响 AI、芯片、新能源等科技资产表现。")
    if "开发者 / 工具 / 开源" in categories:
        lines.append("开发者工具和开源项目是否出现真实迁移、生态扩张或商业化信号。")
    if not lines and clusters:
        lines.append("高分事件是否在后续批次中继续跨来源出现。")
    return lines


def _s3_client_from_config(config: Dict[str, Any]):
    storage = config.get("storage") or {}
    remote = storage.get("remote") or {}
    bucket = os.environ.get("S3_BUCKET_NAME") or remote.get("bucket_name") or remote.get("BUCKET_NAME") or ""
    access_key = os.environ.get("S3_ACCESS_KEY_ID") or remote.get("access_key_id") or remote.get("ACCESS_KEY_ID") or ""
    secret_key = os.environ.get("S3_SECRET_ACCESS_KEY") or remote.get("secret_access_key") or remote.get("SECRET_ACCESS_KEY") or ""
    endpoint_url = os.environ.get("S3_ENDPOINT_URL") or remote.get("endpoint_url") or remote.get("ENDPOINT_URL") or ""
    region = os.environ.get("S3_REGION") or remote.get("region") or remote.get("REGION") or "auto"
    if not (bucket and access_key and secret_key and endpoint_url and boto3 and BotoConfig):
        return None, bucket
    signature_version = "s3" if ("myqcloud.com" in endpoint_url.lower() or "aliyuncs.com" in endpoint_url.lower()) else "s3v4"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=BotoConfig(s3={"addressing_style": "virtual"}, signature_version=signature_version),
    )
    return client, bucket


def _public_url(value: Any) -> str:
    url = _text(value)
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return normalize_url(url) if parts.scheme.lower() in {"http", "https"} and parts.netloc else ""


def _public_summary(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value))[:280]


def _public_digest_summary(value: Any, allowed_ids: set[str]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    top_items = []
    for raw_item in value.get("top_items", []) or []:
        if not isinstance(raw_item, dict):
            continue
        item_id = _text(raw_item.get("id"))
        evidence_ids = [
            _text(item) for item in raw_item.get("evidence_ids", []) or []
            if _text(item) in allowed_ids
        ][:6]
        if item_id not in allowed_ids or item_id not in evidence_ids:
            continue
        top_items.append({
            "id": item_id,
            "headline": _clip(raw_item.get("headline", ""), 24),
            "event": _clip(raw_item.get("event", ""), 56),
            "impact": _clip(raw_item.get("impact", ""), 56),
            "watch": _clip(raw_item.get("watch", ""), 40),
            "evidence_ids": evidence_ids,
        })
    watchlist = []
    for raw_item in value.get("watchlist", []) or []:
        if not isinstance(raw_item, dict):
            continue
        evidence_ids = [
            _text(item) for item in raw_item.get("evidence_ids", []) or []
            if _text(item) in allowed_ids
        ][:6]
        text = _clip(raw_item.get("text", ""), 40)
        if text and evidence_ids:
            watchlist.append({"text": text, "evidence_ids": evidence_ids})
    return {
        "status": "ai" if value.get("status") == "ai" else "rules",
        "overview": _clip(value.get("overview", ""), 80),
        "top_items": top_items[:3],
        "watchlist": watchlist[:3],
    }


def build_public_projection(package: Dict[str, Any]) -> Dict[str, Any]:
    """Build the strict public DTO. Private/raw fields never cross this boundary."""
    records = []
    for item in package.get("raw_items", []):
        records.append({
            "id": item.get("id", ""),
            "short_id": item.get("short_id", ""),
            "date": package.get("date", ""),
            "type": "news",
            "title": item.get("title", ""),
            "source": item.get("source", ""),
            "source_count": max(1, int(item.get("source_count") or 1)),
            "url": _public_url(item.get("url", "")),
            "category": item.get("category", DEFAULT_CATEGORY),
            "tags": list(item.get("tags", []) or [])[:10],
            "score": int(item.get("raw_item_score") or 0),
            "first_seen": item.get("first_seen", ""),
            "last_seen": item.get("last_seen", ""),
            "occurrence_count": int(item.get("occurrence_count") or 1),
            "summary": _public_summary(item.get("summary", "")),
        })
    for cluster in package.get("clusters", []):
        records.append({
            "id": cluster.get("cluster_id", ""),
            "short_id": cluster.get("short_cluster_id", ""),
            "date": package.get("date", ""),
            "type": "event_cluster",
            "title": cluster.get("title", ""),
            "source": f"{int(cluster.get('source_count') or 0)} 个独立来源",
            "source_count": max(1, int(cluster.get("source_count") or 1)),
            "url": "",
            "category": cluster.get("category", DEFAULT_CATEGORY),
            "tags": list(cluster.get("tags", []) or [])[:10],
            "score": int(cluster.get("cluster_score") or 0),
            "first_seen": cluster.get("created_at", ""),
            "last_seen": cluster.get("created_at", ""),
            "occurrence_count": len(cluster.get("related_item_ids", []) or []),
            "summary": _public_summary(cluster.get("summary", "")),
        })
    allowed_ids = {record["id"] for record in records if record.get("id")}
    return {
        "schema_version": 2,
        "date": package.get("date", ""),
        "generated_at": package.get("generated_at", ""),
        "run": {
            "slot": _text(package.get("slot")),
            "source": "ravenis",
            "generated_at": _text(package.get("generated_at")),
            "record_ids": sorted(allowed_ids),
            "summary": _public_digest_summary(package.get("digest_summary"), allowed_ids),
        },
        "records": records,
    }


def archive_external_run_to_r2(
    *,
    date: str,
    slot: str,
    source: str,
    private_run: Dict[str, Any],
    public_records: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> bool:
    """Archive one non-crawler producer using the same run/projection contract."""
    client, bucket = _s3_client_from_config(config)
    if client is None or not bucket:
        print(f"[{source}] R2 archive failed: storage credentials are not configured")
        return False
    invalid = [sorted(set(record) - PUBLIC_HISTORY_FIELDS) for record in public_records]
    invalid = [fields for fields in invalid if fields]
    if invalid:
        raise ValueError(f"public projection contains forbidden fields: {invalid}")
    yyyy, mm, dd = date.split("-")
    run_number = _text(os.environ.get("GITHUB_RUN_ID")) or re.sub(r"\D", "", datetime.now().isoformat())
    run_id = f"{date.replace('-', '')}-{slot}-{run_number or 'local'}"
    run_payload = dict(private_run)
    run_payload.update({"run_id": run_id, "date": date, "slot": slot, "source": source})
    projection = {
        "schema_version": 2,
        "date": date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run": {
            "slot": slot,
            "source": source,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "record_ids": [
                _text(record.get("id")) for record in public_records if _text(record.get("id"))
            ],
            "summary": {},
        },
        "records": public_records,
    }
    try:
        raw = json.dumps(run_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = gzip.compress(raw, compresslevel=6)
        client.put_object(
            Bucket=bucket,
            Key=f"runs/{yyyy}/{mm}/{dd}/{slot}/{run_id}.json.gz",
            Body=compressed,
            ContentLength=len(compressed),
            ContentType="application/json",
            ContentEncoding="gzip",
        )
        body = json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        client.put_object(
            Bucket=bucket,
            Key=f"public-history/{date}/{source}-{slot}.json",
            Body=body,
            ContentLength=len(body),
            ContentType="application/json; charset=utf-8",
        )
        print(f"[{source}] archived run={run_id} public_records={len(public_records)}")
        return True
    except Exception as exc:
        print(f"[{source}] R2 archive failed: {exc}")
        return False


def archive_intelligence_to_r2(package: Dict[str, Any], messages: List[str], config: Optional[Dict[str, Any]] = None) -> bool:
    cfg = config or {}
    archive_cfg = (cfg.get("storage") or {}).get("archive") or {}
    if not archive_cfg.get("enabled", True):
        return False
    client, bucket = _s3_client_from_config(cfg)
    if client is None or not bucket:
        print("[intelligence] R2 archive skipped: storage credentials are not configured")
        return False

    date_parts = package["date"].split("-")
    yyyy, mm, dd = date_parts[0], date_parts[1], date_parts[2]
    slot = package["slot"]
    run_id = _text(os.environ.get("GITHUB_RUN_ID")) or re.sub(r"\D", "", package.get("generated_at", ""))
    run_id = f"{package['date'].replace('-', '')}-{slot}-{run_id or 'local'}"

    def put_json(key: str, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentLength=len(body), ContentType="application/json; charset=utf-8")

    def put_text(key: str, text: str) -> None:
        body = text.encode("utf-8")
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentLength=len(body), ContentType="text/plain; charset=utf-8")

    def put_gzip_json(key: str, obj: Dict[str, Any]) -> None:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        body = gzip.compress(raw, compresslevel=6)
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentLength=len(body),
            ContentType="application/json",
            ContentEncoding="gzip",
        )

    try:
        run_package = dict(package)
        run_package["messages"] = messages
        run_package["run_id"] = run_id
        put_gzip_json(f"runs/{yyyy}/{mm}/{dd}/{slot}/{run_id}.json.gz", run_package)
        put_json(
            f"public-history/{package['date']}/ravenis-{slot}.json",
            build_public_projection(package),
        )

        legacy_until = _text((cfg.get("migration") or {}).get("dual_write_legacy_until"))
        if legacy_until and package["date"] <= legacy_until:
            for item in package.get("raw_items", []):
                put_json(f"news/{yyyy}/{mm}/{dd}/{slot}/{item['id']}.json", item)
            for cluster in package.get("clusters", []):
                put_json(f"clusters/{yyyy}/{mm}/{dd}/{slot}/{cluster['cluster_id']}.json", cluster)
        for index, message in enumerate(messages):
            put_text(f"reports/{yyyy}/{mm}/{dd}/{slot}/wechat_message_{index}.txt", message)
        print(
            "[intelligence] archived run bundle and public projection: "
            f"run_id={run_id} items={len(package.get('raw_items', []))} "
            f"clusters={len(package.get('clusters', []))}"
        )
        return True
    except Exception as exc:
        print(f"[intelligence] R2 archive failed, notification will continue: {exc}")
        return False
