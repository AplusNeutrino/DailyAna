# coding=utf-8
"""Ravenis Core intelligence packaging, rendering, and R2 archiving.

This module is intentionally rule-first.  It converts the existing crawler
output into traceable raw items and lightweight event clusters without changing
the crawler or the old notification path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:  # pragma: no cover - handled at runtime
    boto3 = None
    BotoConfig = None


CATEGORIES = [
    "AI / 模型",
    "芯片 / 算力",
    "自动驾驶 / 机器人",
    "新能源 / 能源",
    "开发者 / 工具 / 开源",
    "宏观 / 财经 / 地缘",
    "国内政策 / 消费 / 社会",
    "娱乐 / 体育 / 游戏",
    "其他",
]

INTENTS = ["WORK_CORE", "WORK_EDGE", "MARKET", "POLICY", "RISK", "CULTURE", "NOISE"]

CATEGORY_RULES: List[Tuple[str, List[str]]] = [
    ("AI / 模型", ["ai", "openai", "gpt", "claude", "gemini", "deepseek", "qwen", "llm", "大模型", "模型", "人工智能", "智谱", "通义", "kimi"]),
    ("芯片 / 算力", ["芯片", "算力", "英伟达", "nvidia", "gpu", "半导体", "台积电", "华为昇腾", "存储", "hbm", "光刻", "液冷"]),
    ("自动驾驶 / 机器人", ["机器人", "自动驾驶", "特斯拉", "tesla", "fsd", "智驾", "宇树", "智元", "小鹏", "蔚来"]),
    ("新能源 / 能源", ["新能源", "电池", "储能", "光伏", "风电", "锂", "能源", "油价", "原油"]),
    ("开发者 / 工具 / 开源", ["github", "开源", "开发者", "编程", "代码", "数据库", "python", "javascript", "工具", "api"]),
    ("宏观 / 财经 / 地缘", ["美联储", "通胀", "美股", "港股", "a股", "黄金", "汇率", "财报", "市场", "中东", "伊朗", "美国", "欧洲", "地缘"]),
    ("国内政策 / 消费 / 社会", ["政策", "监管", "官方", "消费", "食品", "事故", "维权", "社会", "教育", "医疗"]),
    ("娱乐 / 体育 / 游戏", ["游戏", "电竞", "体育", "足球", "篮球", "电影", "影视", "明星", "演唱会", "b站", "bilibili"]),
]

TAG_RULES: List[Tuple[str, List[str]]] = [
    ("DeepSeek", ["deepseek"]),
    ("Qwen", ["qwen", "通义"]),
    ("OpenAI", ["openai", "gpt"]),
    ("Claude", ["claude", "anthropic"]),
    ("Gemini", ["gemini", "google ai"]),
    ("英伟达", ["英伟达", "nvidia"]),
    ("华为昇腾", ["昇腾", "ascend"]),
    ("芯片", ["芯片", "半导体", "gpu", "hbm"]),
    ("机器人", ["机器人", "宇树", "智元"]),
    ("自动驾驶", ["自动驾驶", "fsd", "智驾"]),
    ("特斯拉", ["特斯拉", "tesla"]),
    ("宏观", ["美联储", "通胀", "汇率", "黄金"]),
    ("地缘", ["中东", "伊朗", "美国", "欧洲", "地缘"]),
    ("游戏", ["游戏", "电竞"]),
    ("体育", ["体育", "足球", "篮球"]),
    ("开源", ["开源", "github"]),
    ("开发工具", ["开发者", "编程", "代码", "api", "数据库"]),
]

RISK_WORDS = ["事故", "处罚", "监管", "风险", "召回", "造谣", "刑案", "下架", "封禁", "漏洞"]
POLICY_WORDS = ["政策", "监管", "官方", "法规", "标准", "条例"]
MARKET_WORDS = ["财报", "股", "涨", "跌", "融资", "并购", "价格", "市值", "订单"]
SOCIAL_SOURCE_IDS = {"weibo", "douyin", "tieba", "zhihu", "bilibili-hot-search", "toutiao", "baidu"}
TECH_SOURCE_HINTS = ["hacker", "github", "verge", "arxiv", "tech", "developer", "ruanyifeng", "阮一峰"]
FINANCE_SOURCE_HINTS = ["finance", "wallstreet", "cls", "财联社", "华尔街", "yahoo"]


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_title(title: str) -> str:
    title = re.sub(r"\s+", " ", _text(title)).lower()
    title = re.sub(r"[^\w\u4e00-\u9fff]+", "", title)
    return title


def content_hash(title: str, url: str) -> str:
    base = _text(url) or normalize_title(title)
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:16]


def get_slot(now: datetime, slots: Optional[Dict[str, Any]] = None) -> str:
    configured = slots or {}
    hour = now.hour
    best_slot = ""
    best_hour = -1
    for slot, spec in configured.items():
        time_text = _text((spec or {}).get("time"))
        match = re.match(r"^(\d{1,2}):", time_text)
        if match:
            slot_hour = int(match.group(1))
            if slot_hour <= hour and slot_hour > best_hour:
                best_slot = str(slot).upper()
                best_hour = slot_hour
    if best_slot:
        return best_slot
    if hour < 11:
        return "A"
    if hour < 17:
        return "B"
    return "C"


def _source_badge(source_name: str, source_type: str, source_id: str) -> str:
    text = f"{source_name} {source_type} {source_id}".lower()
    if source_type == "rss":
        if any(x in text for x in TECH_SOURCE_HINTS):
            return "技"
        if any(x in text for x in FINANCE_SOURCE_HINTS):
            return "财"
        if any(x in text for x in ["game", "游戏"]):
            return "游"
        return "RSS"
    if any(x in text for x in FINANCE_SOURCE_HINTS):
        return "财"
    if any(x in text for x in ["gov", "policy", "政务", "官方"]):
        return "政"
    if any(x in text for x in ["game", "游戏"]):
        return "游"
    if any(x in text for x in ["sport", "体育"]):
        return "体"
    if any(x in text for x in ["ent", "娱乐", "bilibili", "weibo", "douyin"]):
        return "娱"
    if source_id in SOCIAL_SOURCE_IDS:
        return "热"
    return "源"


def _classify(title: str, source_name: str, source_type: str) -> Tuple[str, List[str], List[str]]:
    haystack = f"{title} {source_name}".lower()
    category = "其他"
    for candidate, words in CATEGORY_RULES:
        if any(word.lower() in haystack for word in words):
            category = candidate
            break

    tags = [tag for tag, words in TAG_RULES if any(word.lower() in haystack for word in words)]
    if not tags and category != "其他":
        tags.append(category.split(" / ")[0])

    intents: List[str] = []
    if category in {"AI / 模型", "芯片 / 算力", "自动驾驶 / 机器人", "开发者 / 工具 / 开源"}:
        intents.append("WORK_CORE")
    elif category == "新能源 / 能源":
        intents.append("WORK_EDGE")
    if any(word.lower() in haystack for word in MARKET_WORDS):
        intents.append("MARKET")
    if any(word.lower() in haystack for word in POLICY_WORDS):
        intents.append("POLICY")
    if any(word.lower() in haystack for word in RISK_WORDS):
        intents.append("RISK")
    if category == "娱乐 / 体育 / 游戏":
        intents.append("CULTURE")
        if source_type != "rss":
            intents.append("NOISE")
    if not intents:
        intents.append("WORK_EDGE" if category != "其他" else "NOISE")

    return category, tags[:8], sorted(set(intents), key=intents.index)


def _source_quality(source_name: str, source_type: str, source_id: str) -> int:
    text = f"{source_name} {source_id}".lower()
    if source_type == "rss":
        if any(x in text for x in TECH_SOURCE_HINTS):
            return 88
        if any(x in text for x in FINANCE_SOURCE_HINTS):
            return 84
        return 76
    if any(x in text for x in FINANCE_SOURCE_HINTS):
        return 78
    if source_id in SOCIAL_SOURCE_IDS:
        return 52
    return 62


def _score_item(item: Dict[str, Any]) -> int:
    category = item.get("category", "其他")
    intents = set(item.get("intent", []))
    source_quality = _source_quality(item.get("source", ""), item.get("source_type", ""), item.get("source_id", ""))
    relevance = {
        "AI / 模型": 95,
        "芯片 / 算力": 92,
        "自动驾驶 / 机器人": 88,
        "开发者 / 工具 / 开源": 84,
        "新能源 / 能源": 76,
        "宏观 / 财经 / 地缘": 72,
        "国内政策 / 消费 / 社会": 58,
        "娱乐 / 体育 / 游戏": 35,
        "其他": 35,
    }.get(category, 35)
    rank = int(item.get("platform_rank") or 0)
    rank_bonus = max(0, 18 - min(rank, 18)) if rank else 4
    novelty = 60
    impact = 45
    if intents & {"MARKET", "POLICY", "RISK"}:
        impact += 20
    if category in {"AI / 模型", "芯片 / 算力", "自动驾驶 / 机器人"}:
        impact += 14
    noise_penalty = 18 if "NOISE" in intents else 0
    raw = relevance * 0.30 + source_quality * 0.20 + 45 * 0.20 + novelty * 0.15 + impact * 0.15 + rank_bonus - noise_penalty
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


def _dedupe_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    ordered: List[Dict[str, Any]] = []
    for item in candidates:
        title = _text(item.get("title"))
        if not title:
            continue
        key = _text(item.get("url")) or normalize_title(title)
        if not key:
            continue
        if key in seen:
            seen[key].setdefault("duplicate_sources", []).append({
                "source": item.get("source", ""),
                "source_type": item.get("source_type", ""),
                "url": item.get("url", ""),
            })
            continue
        seen[key] = item
        ordered.append(item)
    return ordered


def _cluster_key(item: Dict[str, Any]) -> str:
    strong_tags = [tag for tag in item.get("tags", []) if tag not in {"AI", "芯片", "宏观"}]
    if strong_tags:
        return f"{item.get('category')}|{strong_tags[0]}"
    norm = normalize_title(item.get("title", ""))
    return f"{item.get('category')}|{norm[:10]}"


def _make_cluster_title(category: str, tags: List[str], items: List[Dict[str, Any]]) -> str:
    if tags:
        return f"{' / '.join(tags[:2])} 相关信号"
    if category and category != "其他":
        return f"{category} 相关信号"
    return _text(items[0].get("title"))[:34] or "未命名事件"


def _build_clusters(items: List[Dict[str, Any]], date_compact: str, date_text: str, slot: str, now: datetime,
                    thresholds: Dict[str, Any]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item.get("raw_item_score", 0) >= int(thresholds.get("list", 30)):
            groups[_cluster_key(item)].append(item)

    clusters: List[Dict[str, Any]] = []
    seq = 1
    for _key, group in sorted(groups.items(), key=lambda kv: max(x.get("raw_item_score", 0) for x in kv[1]), reverse=True):
        if len(group) == 1 and group[0].get("raw_item_score", 0) < int(thresholds.get("altar", 80)):
            continue
        tags: List[str] = []
        for item in group:
            for tag in item.get("tags", []):
                if tag not in tags:
                    tags.append(tag)
        sources = {item.get("source_id") or item.get("source") for item in group}
        base_score = max(item.get("raw_item_score", 0) for item in group)
        cluster_score = min(100, base_score + min(12, max(0, len(sources) - 1) * 4) + min(8, max(0, len(group) - 1) * 2))
        cluster_id = f"{date_compact}{slot}C{seq:02d}"
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
            "signal_strength": "高" if cluster_score >= 80 else "中高" if cluster_score >= 65 else "中",
            "summary": summary,
            "observation": _cluster_observation(group[0].get("category", "其他"), tags),
            "created_at": now.isoformat(timespec="seconds"),
        }
        for item in group:
            item["cluster_id"] = cluster_id
        clusters.append(cluster)
        seq += 1
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
    now = now or datetime.now()
    date_text = now.date().isoformat()
    date_compact = now.strftime("%Y%m%d")
    slot = get_slot(now, cfg.get("slots"))
    thresholds = (cfg.get("scoring") or {}).get("thresholds") or {"altar": 80, "brief": 60, "list": 30}

    raw_candidates = _dedupe_candidates(_collect_raw_candidates(report_data, rss_items, rss_new_items, standalone_data))
    raw_items: List[Dict[str, Any]] = []
    for seq, candidate in enumerate(raw_candidates, 1):
        title = _text(candidate.get("title"))
        source = _text(candidate.get("source"))
        source_type = _text(candidate.get("source_type")) or "hotlist"
        source_id = _text(candidate.get("source_id"))
        category, tags, intent = _classify(title, source, source_type)
        item = {
            "id": f"{date_compact}{slot}{seq:03d}",
            "short_id": f"{slot}{seq:03d}",
            "date": date_text,
            "slot": slot,
            "seq": seq,
            "title": title,
            "normalized_title": normalize_title(title),
            "url": _text(candidate.get("url")),
            "source": source,
            "source_id": source_id,
            "source_type": source_type,
            "source_badge": _source_badge(source, source_type, source_id),
            "platform_rank": int(candidate.get("platform_rank") or 0),
            "captured_at": _text(candidate.get("captured_at")) or now.isoformat(timespec="seconds"),
            "category": category,
            "tags": tags,
            "intent": intent,
            "cluster_id": "",
            "content_hash": content_hash(title, _text(candidate.get("url"))),
            "summary": _text(candidate.get("summary")),
            "duplicate_sources": candidate.get("duplicate_sources", []),
            "raw_payload": candidate.get("raw_payload", {}),
        }
        item["raw_item_score"] = _score_item(item)
        raw_items.append(item)

    clusters = _build_clusters(raw_items, date_compact, date_text, slot, now, thresholds)
    return {
        "date": date_text,
        "date_compact": date_compact,
        "slot": slot,
        "generated_at": now.isoformat(timespec="seconds"),
        "thresholds": thresholds,
        "raw_items": raw_items,
        "clusters": clusters,
    }


def _clip(text: str, limit: int = 88) -> str:
    text = re.sub(r"\s+", " ", _text(text))
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _category_groups(items: List[Dict[str, Any]], min_score: int, max_per_category: int) -> List[Tuple[str, List[Dict[str, Any]]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item.get("raw_item_score", 0) >= min_score:
            grouped[item.get("category", "其他")].append(item)
    ordered = []
    for category in CATEGORIES:
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


def render_wechat_intelligence_messages(package: Dict[str, Any], config: Optional[Dict[str, Any]] = None,
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
    header = f"Ravenis Core · Normai Tracking Details\n{display_time} · {package.get('slot')} · 智能新闻速递"

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
    for category, group in _category_groups(raw_items, list_threshold, max_items_per_category):
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

    def put_json(key: str, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentLength=len(body), ContentType="application/json; charset=utf-8")

    def put_text(key: str, text: str) -> None:
        body = text.encode("utf-8")
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentLength=len(body), ContentType="text/plain; charset=utf-8")

    try:
        for item in package.get("raw_items", []):
            put_json(f"news/{yyyy}/{mm}/{dd}/{slot}/{item['id']}.json", item)
        for cluster in package.get("clusters", []):
            put_json(f"clusters/{yyyy}/{mm}/{dd}/{slot}/{cluster['cluster_id']}.json", cluster)
        for index, message in enumerate(messages):
            put_text(f"reports/{yyyy}/{mm}/{dd}/{slot}/wechat_message_{index}.txt", message)
        print(f"[intelligence] archived {len(package.get('raw_items', []))} raw items and {len(package.get('clusters', []))} clusters to R2")
        return True
    except Exception as exc:
        print(f"[intelligence] R2 archive failed, notification will continue: {exc}")
        return False
