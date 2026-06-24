#!/usr/bin/env python3
# coding=utf-8
"""
Build a public, lightweight history search index from remote daily SQLite files.

The index intentionally contains only non-secret fields:
date, title, source, source id, url, item type, and crawl/published timestamps.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:  # pragma: no cover - handled at runtime in Actions
    boto3 = None
    BotoConfig = None


DATE_RE = re.compile(r"^(news|rss)/(\d{4}-\d{2}-\d{2})\.db$")
NEWS_JSON_RE = re.compile(r"^news/(\d{4})/(\d{2})/(\d{2})/([A-Z])/([^/]+)\.json$")
CLUSTER_JSON_RE = re.compile(r"^clusters/(\d{4})/(\d{2})/(\d{2})/([A-Z])/([^/]+)\.json$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_empty_index(retention_days: int) -> dict[str, Any]:
    now = utc_now()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "retention_days": retention_days,
        "total": 0,
        "items": [],
        "sources": [],
        "note": "R2 credentials are not configured yet, so no remote history index was built.",
    }


def s3_client_from_env():
    required = ["S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing or boto3 is None:
        return None, os.environ.get("S3_BUCKET_NAME", ""), missing

    endpoint_url = os.environ["S3_ENDPOINT_URL"]
    region = os.environ.get("S3_REGION") or "auto"
    signature_version = "s3" if ("myqcloud.com" in endpoint_url.lower() or "aliyuncs.com" in endpoint_url.lower()) else "s3v4"
    config = BotoConfig(s3={"addressing_style": "virtual"}, signature_version=signature_version)
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=region,
        config=config,
    )
    return client, os.environ["S3_BUCKET_NAME"], []


def list_db_keys(client, bucket: str) -> list[tuple[str, str, str]]:
    keys: list[tuple[str, str, str]] = []
    paginator = client.get_paginator("list_objects_v2")
    for prefix in ("news/", "rss/"):
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                match = DATE_RE.match(key)
                if match:
                    keys.append((key, match.group(1), match.group(2)))
    return keys


def list_intelligence_json_keys(client, bucket: str) -> list[tuple[str, str, str]]:
    keys: list[tuple[str, str, str]] = []
    paginator = client.get_paginator("list_objects_v2")
    for prefix, kind, pattern in (
        ("news/", "intelligence_news", NEWS_JSON_RE),
        ("clusters/", "intelligence_cluster", CLUSTER_JSON_RE),
    ):
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                match = pattern.match(key)
                if match:
                    date_str = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
                    keys.append((key, kind, date_str))
    return keys


def download_object(client, bucket: str, key: str, target: Path) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        with target.open("wb") as f:
            for chunk in response["Body"].iter_chunks(chunk_size=1024 * 1024):
                f.write(chunk)
        return True
    except Exception as exc:
        print(f"[history] download failed: {key}: {exc}")
        return False


def normalize_url(url: str) -> str:
    return (url or "").strip()


def add_item(items: list[dict[str, Any]], seen: set[str], item: dict[str, Any]) -> None:
    url = normalize_url(item.get("url", ""))
    title = (item.get("title") or "").strip()
    source_id = item.get("source_id") or ""
    dedupe_key = item.get("id") or url or f"{item.get('type')}|{source_id}|{title}"
    if not title or dedupe_key in seen:
        return
    seen.add(dedupe_key)
    item["search_text"] = " ".join(
        str(item.get(key, ""))
        for key in (
            "id", "short_id", "date", "title", "source", "source_id",
            "type", "category", "cluster_id", "summary", "content",
            "sentence", "playbook", "significance", "overall_observation",
        )
    ).lower()
    item["search_text"] += " " + " ".join(item.get("tags", []) or []).lower()
    item["search_text"] += " " + " ".join(item.get("intent", []) or []).lower()
    item["search_text"] += " " + " ".join(item.get("retrieval_keywords", []) or []).lower()
    item["search_text"] += " " + " ".join(item.get("entities", []) or []).lower()
    item["search_text"] += " " + " ".join(item.get("source_urls", []) or []).lower()
    items.append(item)


def parse_json_list(value: str) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def extract_intelligence_json(local_path: Path, date_str: str, kind: str,
                              items: list[dict[str, Any]], seen: set[str]) -> None:
    try:
        data = json.loads(local_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[history] read json failed: {local_path}: {exc}")
        return

    if kind == "intelligence_news":
        add_item(items, seen, {
            "id": data.get("id", ""),
            "short_id": data.get("short_id", ""),
            "date": data.get("date") or date_str,
            "slot": data.get("slot", ""),
            "type": "intelligence",
            "title": data.get("title", ""),
            "source": data.get("source", ""),
            "source_id": data.get("source_id", ""),
            "source_type": data.get("source_type", ""),
            "url": data.get("url", ""),
            "category": data.get("category", ""),
            "tags": data.get("tags", []),
            "intent": data.get("intent", []),
            "score": data.get("raw_item_score", 0),
            "cluster_id": data.get("cluster_id", ""),
            "published_at": data.get("captured_at", ""),
            "first_seen": data.get("captured_at", ""),
            "last_seen": data.get("captured_at", ""),
        })
        return

    add_item(items, seen, {
        "id": data.get("cluster_id", ""),
        "short_id": data.get("short_cluster_id", ""),
        "date": data.get("date") or date_str,
        "slot": data.get("slot", ""),
        "type": "cluster",
        "title": data.get("title", ""),
        "source": "Ravenis Core",
        "source_id": "cluster",
        "url": "",
        "category": data.get("category", ""),
        "tags": data.get("tags", []),
        "intent": [],
        "score": data.get("cluster_score", 0),
        "cluster_id": data.get("cluster_id", ""),
        "related_item_ids": data.get("related_item_ids", []),
        "summary": data.get("summary", ""),
        "observation": data.get("observation", ""),
        "published_at": data.get("created_at", ""),
        "first_seen": data.get("created_at", ""),
        "last_seen": data.get("created_at", ""),
    })


def extract_news(db_path: Path, date_str: str, items: list[dict[str, Any]], seen: set[str]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT n.title, n.url, n.mobile_url, n.platform_id, p.name AS source_name,
                   n.first_crawl_time, n.last_crawl_time
            FROM news_items n
            LEFT JOIN platforms p ON n.platform_id = p.id
            """
        ).fetchall()
        for row in rows:
            add_item(items, seen, {
                "date": date_str,
                "type": "hotlist",
                "title": row["title"],
                "source": row["source_name"] or row["platform_id"],
                "source_id": row["platform_id"],
                "url": row["url"] or row["mobile_url"] or "",
                "published_at": "",
                "first_seen": row["first_crawl_time"],
                "last_seen": row["last_crawl_time"],
            })
    except sqlite3.Error as exc:
        print(f"[history] read news failed: {db_path}: {exc}")
    finally:
        conn.close()


def extract_rss(db_path: Path, date_str: str, items: list[dict[str, Any]], seen: set[str]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT i.title, i.url, i.feed_id, f.name AS source_name,
                   i.published_at, i.first_crawl_time, i.last_crawl_time
            FROM rss_items i
            LEFT JOIN rss_feeds f ON i.feed_id = f.id
            WHERE i.feed_id != 'ai-digest-daily'
            """
        ).fetchall()
        for row in rows:
            add_item(items, seen, {
                "date": date_str,
                "type": "rss",
                "title": row["title"],
                "source": row["source_name"] or row["feed_id"],
                "source_id": row["feed_id"],
                "url": row["url"] or "",
                "published_at": row["published_at"] or "",
                "first_seen": row["first_crawl_time"],
                "last_seen": row["last_crawl_time"],
            })
        extract_ai_digest(conn, date_str, items, seen)
    except sqlite3.Error as exc:
        print(f"[history] read rss failed: {db_path}: {exc}")
    finally:
        conn.close()


def extract_ai_digest(conn: sqlite3.Connection, date_str: str, items: list[dict[str, Any]], seen: set[str]) -> None:
    try:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_digest_items'"
        ).fetchone()
        if not table_exists:
            return
        rows = conn.execute(
            """
            SELECT i.digest_id, i.item_index, i.title, i.page_url, i.primary_url,
                   i.published_at, i.sentence, i.link_text, i.playbook,
                   i.significance, i.use_cases_json, i.source_urls_json,
                   i.full_text, i.content_hash, i.first_crawl_time, i.last_crawl_time,
                   a.status AS analysis_status, a.summary AS analysis_summary,
                   a.key_points_json, a.category, a.tags_json, a.entities_json,
                   a.retrieval_keywords_json,
                   d.summary AS daily_summary, d.overall_observation
            FROM ai_digest_items i
            LEFT JOIN ai_digest_item_analysis a ON a.digest_id = i.digest_id
            LEFT JOIN ai_digest_daily_analysis d ON d.date = i.date
            WHERE i.date = ?
            ORDER BY i.item_index ASC
            """,
            (date_str,),
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"[history] read ai digest failed: {exc}")
        return

    for row in rows:
        use_cases = parse_json_list(row["use_cases_json"])
        source_urls = parse_json_list(row["source_urls_json"])
        tags = parse_json_list(row["tags_json"])
        entities = parse_json_list(row["entities_json"])
        retrieval_keywords = parse_json_list(row["retrieval_keywords_json"])
        key_points = parse_json_list(row["key_points_json"])
        content_parts = [
            row["full_text"] or "",
            row["analysis_summary"] or "",
            " ".join(key_points),
            " ".join(tags),
            " ".join(entities),
            " ".join(retrieval_keywords),
            row["daily_summary"] or "",
            row["overall_observation"] or "",
        ]
        add_item(items, seen, {
            "id": row["digest_id"],
            "short_id": f"ai-digest-{date_str}-{row['item_index']}",
            "date": date_str,
            "type": "ai_digest",
            "title": row["title"],
            "source": "AI Digest Daily",
            "source_id": "ai-digest-daily",
            "url": row["primary_url"] or row["page_url"],
            "page_url": row["page_url"],
            "published_at": row["published_at"] or "",
            "first_seen": row["first_crawl_time"],
            "last_seen": row["last_crawl_time"],
            "sentence": row["sentence"] or "",
            "link_text": row["link_text"] or "",
            "playbook": row["playbook"] or "",
            "significance": row["significance"] or "",
            "use_cases": use_cases,
            "source_urls": source_urls,
            "content_hash": row["content_hash"] or "",
            "content": "\n".join(part for part in content_parts if part).strip(),
            "summary": row["analysis_summary"] or row["sentence"] or "",
            "key_points": key_points,
            "category": row["category"] or "",
            "tags": tags,
            "entities": entities,
            "retrieval_keywords": retrieval_keywords,
            "analysis_status": row["analysis_status"] or "",
        })


def prune_remote(client, bucket: str, keys: list[tuple[str, str, str]], cutoff_date: str) -> int:
    deleted = 0
    for key, _kind, date_str in keys:
        if date_str >= cutoff_date:
            continue
        try:
            client.delete_object(Bucket=bucket, Key=key)
            deleted += 1
            print(f"[history] deleted expired remote db: {key}")
        except Exception as exc:
            print(f"[history] delete failed: {key}: {exc}")
    return deleted


def upload_index(client, bucket: str, key: str, index_path: Path) -> None:
    body = index_path.read_bytes()
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentLength=len(body),
        ContentType="application/json; charset=utf-8",
        CacheControl="public, max-age=300",
    )
    print(f"[history] uploaded index: {key}")


def build_index(retention_days: int, output: Path, upload_key: str, prune: bool) -> int:
    client, bucket, missing = s3_client_from_env()
    if client is None:
        if missing:
            print(f"[history] R2 secrets missing: {', '.join(missing)}")
        elif boto3 is None:
            print("[history] boto3 is not installed")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(make_empty_index(retention_days), ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    now = utc_now()
    cutoff = (now - timedelta(days=retention_days - 1)).date().isoformat()
    keys = list_db_keys(client, bucket)
    json_keys = list_intelligence_json_keys(client, bucket)
    recent_keys = [(key, kind, date_str) for key, kind, date_str in keys if date_str >= cutoff]
    recent_json_keys = [(key, kind, date_str) for key, kind, date_str in json_keys if date_str >= cutoff]

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="ravenis_history_") as tmp:
        tmp_dir = Path(tmp)
        for key, kind, date_str in sorted(recent_keys, key=lambda x: (x[2], x[1])):
            local = tmp_dir / key.replace("/", "_")
            if not download_object(client, bucket, key, local):
                continue
            if kind == "news":
                extract_news(local, date_str, items, seen)
            else:
                extract_rss(local, date_str, items, seen)
        for key, kind, date_str in sorted(recent_json_keys, key=lambda x: (x[2], x[1], x[0])):
            local = tmp_dir / key.replace("/", "_")
            if not download_object(client, bucket, key, local):
                continue
            extract_intelligence_json(local, date_str, kind, items, seen)

    items.sort(key=lambda x: (x.get("date", ""), x.get("published_at") or x.get("last_seen") or ""), reverse=True)
    sources = sorted({f"{item['type']}:{item['source_id']}:{item['source']}" for item in items})
    index = {
        "generated_at": now.isoformat(timespec="seconds"),
        "retention_days": retention_days,
        "total": len(items),
        "sources": sources,
        "items": items,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[history] wrote index: {output} ({len(items)} items)")

    if upload_key:
        upload_index(client, bucket, upload_key, output)
    if prune:
        deleted = prune_remote(client, bucket, keys + json_keys, cutoff)
        print(f"[history] pruned {deleted} expired remote history files")
    return len(items)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Ravenis Core history search index")
    parser.add_argument("--retention-days", type=int, default=int(os.environ.get("HISTORY_RETENTION_DAYS", "30")))
    parser.add_argument("--output", default="docs/history/history-index.json")
    parser.add_argument("--upload-key", default=os.environ.get("HISTORY_INDEX_KEY", "history/history-index.json"))
    parser.add_argument("--prune-remote", action="store_true")
    args = parser.parse_args()

    build_index(
        retention_days=max(args.retention_days, 1),
        output=Path(args.output),
        upload_key=args.upload_key,
        prune=args.prune_remote,
    )


if __name__ == "__main__":
    main()
