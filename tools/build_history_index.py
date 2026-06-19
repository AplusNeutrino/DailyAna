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
    dedupe_key = url or f"{item.get('type')}|{source_id}|{title}"
    if not title or dedupe_key in seen:
        return
    seen.add(dedupe_key)
    item["search_text"] = " ".join(
        str(item.get(key, ""))
        for key in ("date", "title", "source", "source_id", "type")
    ).lower()
    items.append(item)


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
    except sqlite3.Error as exc:
        print(f"[history] read rss failed: {db_path}: {exc}")
    finally:
        conn.close()


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
    recent_keys = [(key, kind, date_str) for key, kind, date_str in keys if date_str >= cutoff]

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
        deleted = prune_remote(client, bucket, keys, cutoff)
        print(f"[history] pruned {deleted} expired remote db files")
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
