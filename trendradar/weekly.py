# coding=utf-8
"""Deterministic weekly intelligence synthesis over validated public records."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List

GENERIC_TAGS = {"AI", "芯片", "宏观", "地缘", "其他"}
STATUS_LABELS = {
    "new": "本周新增",
    "reinforced": "持续增强",
    "stable": "保持稳定",
    "cooled": "明显降温",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _record_date(record: Dict[str, Any]) -> str:
    return _text(record.get("date"))[:10]


def _theme_name(record: Dict[str, Any]) -> str:
    for tag in record.get("tags", []) or []:
        value = _text(tag)
        if value and value not in GENERIC_TAGS:
            return value
    return _text(record.get("category")) or "其他"


def _theme_status(early_count: int, recent_count: int) -> str:
    if early_count == 0 and recent_count > 0:
        return "new"
    if recent_count >= max(2, int(early_count * 1.5)):
        return "reinforced"
    if early_count > 0 and recent_count <= early_count * 0.5:
        return "cooled"
    return "stable"


def build_weekly_digest(
    records: Iterable[Dict[str, Any]],
    *,
    end_date: str = "",
    window_days: int = 7,
    max_themes: int = 12,
) -> Dict[str, Any]:
    raw_records = [record for record in records if _record_date(record)]
    if not raw_records:
        raise ValueError("weekly digest requires at least one dated record")
    last_date = end_date or max(_record_date(record) for record in raw_records)
    end = datetime.strptime(last_date, "%Y-%m-%d").date()
    start = end - timedelta(days=max(1, window_days) - 1)
    selected = [record for record in raw_records if start.isoformat() <= _record_date(record) <= end.isoformat()]
    if not selected:
        raise ValueError("weekly digest date window contains no records")
    midpoint = start + timedelta(days=max(1, window_days) // 2)

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in selected:
        grouped[_theme_name(record)].append(record)

    themes = []
    for name, items in grouped.items():
        early = [item for item in items if _record_date(item) < midpoint.isoformat()]
        recent = [item for item in items if _record_date(item) >= midpoint.isoformat()]
        status = _theme_status(len(early), len(recent))
        evidence = sorted(
            items,
            key=lambda item: (int(item.get("score") or 0), _record_date(item), _text(item.get("last_seen"))),
            reverse=True,
        )
        sources = sorted({_text(item.get("source")) for item in items if _text(item.get("source"))})
        max_score = max(int(item.get("score") or 0) for item in items)
        rank = max_score + min(len(items), 10) * 2 + (5 if status in {"new", "reinforced"} else 0)
        themes.append({
            "id": "w_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16],
            "name": name,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "category": _text(evidence[0].get("category")) or "其他",
            "record_count": len(items),
            "source_count": len(sources),
            "max_score": max_score,
            "first_seen": min(_record_date(item) for item in items),
            "last_seen": max(_record_date(item) for item in items),
            "evidence_ids": list(dict.fromkeys(_text(item.get("id")) for item in evidence if _text(item.get("id"))))[:6],
            "headline": f"{name}：{STATUS_LABELS[status]}",
            "impact": f"涉及 {len(items)} 条公开记录、{len(sources)} 个来源，最高评分 {max_score}。",
            "watch": f"下周观察“{name}”是否出现新的独立来源、正式数据或更高评分记录。",
            "_rank": rank,
        })
    themes.sort(key=lambda item: (item["_rank"], item["last_seen"], item["name"]), reverse=True)
    themes = themes[: max(1, min(int(max_themes or 12), 12))]
    for theme in themes:
        theme.pop("_rank", None)
    sections = {
        status: [theme for theme in themes if theme["status"] == status]
        for status in ("new", "reinforced", "stable", "cooled")
    }
    top_themes = themes[:3]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "record_count": len(selected),
        "theme_count": len(themes),
        "top_themes": top_themes,
        "sections": sections,
        "watchlist": [theme["watch"] for theme in top_themes],
        "model_candidates": themes[:12],
    }


def weekly_intelligence_package(digest: Dict[str, Any]) -> Dict[str, Any]:
    """Expose at most 12 structured themes to the existing one-call digest analyzer."""
    raw_items = []
    for theme in (digest.get("model_candidates") or [])[:12]:
        raw_items.append({
            "id": theme.get("id", ""),
            "title": theme.get("headline", ""),
            "source": f"{int(theme.get('source_count') or 0)} 个公开来源",
            "source_type": "weekly_theme",
            "category": theme.get("category", ""),
            "tags": [theme.get("name", "")],
            "raw_item_score": theme.get("max_score", 0),
            "source_count": theme.get("source_count", 0),
            "occurrence_count": theme.get("record_count", 0),
            "first_seen": theme.get("first_seen", ""),
            "last_seen": theme.get("last_seen", ""),
            "score_components": {},
            "sources": [{"source": f"{int(theme.get('source_count') or 0)} 个公开来源"}],
        })
    return {
        "date": digest.get("end_date", ""),
        "slot": "WEEKLY",
        "raw_items": raw_items,
        "thresholds": {"list": 0},
        "category_order": list(dict.fromkeys(item.get("category", "") for item in raw_items)),
    }


def render_weekly_markdown(digest: Dict[str, Any], detail_url: str = "") -> str:
    start = _text(digest.get("start_date"))
    end = _text(digest.get("end_date"))
    lines = [
        f"# Ravenis 周报 · {start} 至 {end}",
        f"> {int(digest.get('record_count') or 0)} 条记录 · {int(digest.get('theme_count') or 0)} 个主题",
    ]
    editorial = digest.get("editorial_summary") or {}
    editorial_top = editorial.get("top_items") or []
    top_themes = digest.get("top_themes", []) or []
    if editorial_top:
        lines.extend(["", "## 本周主线"])
        for index, item in enumerate(editorial_top, 1):
            lines.extend([
                "",
                f"**{index}. {_text(item.get('headline'))}**",
                f"发生：{_text(item.get('event'))}",
                f"影响：{_text(item.get('impact'))}",
                f"观察：{_text(item.get('watch'))}",
            ])
    elif top_themes:
        lines.extend(["", "## 本周主线"])
        for index, theme in enumerate(top_themes, 1):
            lines.extend([
                "",
                f"**{index}. {_text(theme.get('headline'))}**",
                f"影响：{_text(theme.get('impact'))}",
                f"观察：{_text(theme.get('watch'))}",
            ])
    for status, title in (("new", "本周新增"), ("reinforced", "持续增强"), ("cooled", "明显降温")):
        themes = (digest.get("sections") or {}).get(status, []) or []
        if themes:
            lines.extend(["", f"## {title}", ""])
            lines.extend(f"• {_text(theme.get('name'))} · {int(theme.get('record_count') or 0)} 条" for theme in themes[:3])
    if detail_url:
        lines.extend(["", f"[查看本周完整证据]({detail_url}) →"])
    if editorial.get("status") != "ai":
        lines.extend(["", "`规则周报 · AI 摘要暂不可用`"])
    return "\n".join(lines).strip()
