#!/usr/bin/env python3
"""One-time legacy R2 to strict public-history projection migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

try:
    from tools.build_history_index import (
        s3_client_from_env,
        safe_http_url,
        utc_now,
        validate_record,
    )
except ModuleNotFoundError:  # direct script execution
    from build_history_index import (  # type: ignore[no-redef]
        s3_client_from_env,
        safe_http_url,
        utc_now,
        validate_record,
    )


DB_RE = re.compile(r"^(news|rss)/(\d{4}-\d{2}-\d{2})\.db$")
NEWS_RE = re.compile(r"^news/(\d{4})/(\d{2})/(\d{2})/([A-Z])/([^/]+)\.json$")
CLUSTER_RE = re.compile(r"^clusters/(\d{4})/(\d{2})/(\d{2})/([A-Z])/([^/]+)\.json$")


def stable_id(title: str, url: str, prefix: str = "r") -> str:
    seed = safe_http_url(url) or re.sub(r"\W+", "", title.lower())
    return f"{prefix}_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def record(
    *,
    item_id: str,
    date: str,
    item_type: str,
    title: str,
    source: str,
    source_count: int = 1,
    url: str = "",
    short_id: str = "",
    category: str = "",
    tags: list[str] | None = None,
    score: int = 0,
    first_seen: str = "",
    last_seen: str = "",
    occurrence_count: int = 1,
    summary: str = "",
) -> dict[str, Any]:
    return validate_record(
        {
            "id": item_id,
            "short_id": short_id,
            "date": date,
            "type": item_type,
            "title": title,
            "source": source,
            "source_count": max(1, source_count),
            "url": url,
            "category": category,
            "tags": tags or [],
            "score": score,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "occurrence_count": occurrence_count,
            "summary": summary,
        },
        "legacy migration",
    )


def extract_news_db(path: Path, date: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT n.title, n.url, n.mobile_url, n.platform_id,
                   p.name AS source_name, n.first_crawl_time, n.last_crawl_time
            FROM news_items n LEFT JOIN platforms p ON n.platform_id = p.id
            """
        ).fetchall()
        return [
            record(
                item_id=stable_id(row["title"], row["url"] or row["mobile_url"] or ""),
                date=date,
                item_type="hotlist",
                title=row["title"],
                source=row["source_name"] or row["platform_id"],
                url=row["url"] or row["mobile_url"] or "",
                first_seen=row["first_crawl_time"] or "",
                last_seen=row["last_crawl_time"] or "",
            )
            for row in rows
            if row["title"]
        ]
    finally:
        connection.close()


def _json_list(value: str) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def extract_rss_db(path: Path, date: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    records = []
    try:
        rows = connection.execute(
            """
            SELECT i.title, i.url, i.feed_id, f.name AS source_name,
                   i.published_at, i.first_crawl_time, i.last_crawl_time
            FROM rss_items i LEFT JOIN rss_feeds f ON i.feed_id = f.id
            WHERE i.feed_id != 'ai-digest-daily'
            """
        ).fetchall()
        for row in rows:
            if not row["title"]:
                continue
            records.append(record(
                item_id=stable_id(row["title"], row["url"] or ""),
                date=date,
                item_type="rss",
                title=row["title"],
                source=row["source_name"] or row["feed_id"],
                url=row["url"] or "",
                first_seen=row["first_crawl_time"] or row["published_at"] or "",
                last_seen=row["last_crawl_time"] or row["published_at"] or "",
            ))

        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_digest_items'"
        ).fetchone()
        if table:
            digest_rows = connection.execute(
                """
                SELECT i.digest_id, i.item_index, i.title, i.primary_url, i.page_url,
                       i.published_at, i.sentence, i.first_crawl_time, i.last_crawl_time,
                       a.summary, a.category, a.tags_json
                FROM ai_digest_items i
                LEFT JOIN ai_digest_item_analysis a ON a.digest_id = i.digest_id
                WHERE i.date = ?
                """,
                (date,),
            ).fetchall()
            for row in digest_rows:
                records.append(record(
                    item_id=row["digest_id"],
                    short_id=f"D{int(row['item_index'] or 0):03d}",
                    date=date,
                    item_type="ai_digest",
                    title=row["title"],
                    source="AI Digest Daily",
                    url=row["primary_url"] or row["page_url"] or "",
                    category=row["category"] or "AI / 模型",
                    tags=_json_list(row["tags_json"])[:10],
                    first_seen=row["first_crawl_time"] or row["published_at"] or "",
                    last_seen=row["last_crawl_time"] or row["published_at"] or "",
                    summary=row["summary"] or row["sentence"] or "",
                ))
    finally:
        connection.close()
    return records


def extract_legacy_json(data: dict[str, Any], date: str, kind: str) -> dict[str, Any] | None:
    if kind == "news":
        title = str(data.get("title") or "").strip()
        if not title:
            return None
        return record(
            item_id=stable_id(title, data.get("url", "")),
            short_id=data.get("short_id", ""),
            date=date,
            item_type="news",
            title=title,
            source=data.get("source", ""),
            url=data.get("url", ""),
            category=data.get("category", ""),
            tags=(data.get("tags") or [])[:10],
            score=int(data.get("raw_item_score") or 0),
            first_seen=data.get("first_seen") or data.get("captured_at") or "",
            last_seen=data.get("last_seen") or data.get("captured_at") or "",
            occurrence_count=int(data.get("occurrence_count") or 1),
            summary=data.get("summary", ""),
        )
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    source_count = int(data.get("source_count") or 0)
    related_item_ids = list(dict.fromkeys(
        str(value).strip() for value in (data.get("related_item_ids") or []) if str(value).strip()
    ))
    if source_count < 2 or len(related_item_ids) < 2:
        return None
    return record(
        item_id=data.get("cluster_id") or stable_id(title, "", "c"),
        short_id=data.get("short_cluster_id", ""),
        date=date,
        item_type="event_cluster",
        title=title,
        source=f"{source_count} 个独立来源",
        source_count=source_count,
        category=data.get("category", ""),
        tags=(data.get("tags") or [])[:10],
        score=int(data.get("cluster_score") or 0),
        first_seen=data.get("created_at", ""),
        last_seen=data.get("created_at", ""),
        occurrence_count=len(related_item_ids),
        summary=data.get("summary", ""),
    )


def list_legacy_keys(client, bucket: str, cutoff: str) -> list[tuple[str, str, str]]:
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for prefix in ("news/", "rss/", "clusters/"):
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item.get("Key", "")
                db_match = DB_RE.match(key)
                news_match = NEWS_RE.match(key)
                cluster_match = CLUSTER_RE.match(key)
                if db_match and db_match.group(2) >= cutoff:
                    keys.append((key, f"{db_match.group(1)}_db", db_match.group(2)))
                elif news_match:
                    date = "-".join(news_match.group(index) for index in (1, 2, 3))
                    if date >= cutoff:
                        keys.append((key, "news", date))
                elif cluster_match:
                    date = "-".join(cluster_match.group(index) for index in (1, 2, 3))
                    if date >= cutoff:
                        keys.append((key, "cluster", date))
    return sorted(keys, key=lambda value: (value[2], value[0]))


def migrate(retention_days: int, dry_run: bool = False) -> dict[str, Any]:
    client, bucket = s3_client_from_env()
    cutoff = (utc_now() - timedelta(days=retention_days - 1)).date().isoformat()
    keys = list_legacy_keys(client, bucket, cutoff)
    by_date: dict[str, list[dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory(prefix="ravenis_legacy_migration_") as temporary:
        temp_dir = Path(temporary)
        for index, (key, kind, date) in enumerate(keys):
            response = client.get_object(Bucket=bucket, Key=key)
            payload = response["Body"].read()
            if kind.endswith("_db"):
                local = temp_dir / f"{index}.db"
                local.write_bytes(payload)
                extracted = extract_news_db(local, date) if kind == "news_db" else extract_rss_db(local, date)
                by_date.setdefault(date, []).extend(extracted)
            else:
                migrated = extract_legacy_json(json.loads(payload.decode("utf-8")), date, kind)
                if migrated:
                    by_date.setdefault(date, []).append(migrated)

    report = {"cutoff": cutoff, "legacy_objects": len(keys), "days": {}}
    for date, records in sorted(by_date.items()):
        deduped = {item["id"]: item for item in records}
        projection = {
            "schema_version": 1,
            "date": date,
            "generated_at": utc_now().isoformat(timespec="seconds"),
            "records": list(deduped.values()),
        }
        body = json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        key = f"public-history/{date}/legacy-MIGRATION.json"
        if not dry_run:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentLength=len(body),
                ContentType="application/json; charset=utf-8",
            )
            remote_body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            if hashlib.sha256(remote_body).hexdigest() != digest:
                raise RuntimeError(f"SHA-256 verification failed after upload: {key}")
        report["days"][date] = {"records": len(deduped), "sha256": digest, "key": key}
        print(f"[migration] {date}: records={len(deduped)} sha256={digest}")
    report["total_records"] = sum(day["records"] for day in report["days"].values())
    output = Path("output/migration-public-history-manifest.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[migration] objects={len(keys)} days={len(by_date)} records={report['total_records']}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        report = migrate(max(1, args.retention_days), dry_run=args.dry_run)
        return 0 if report["days"] else 1
    except Exception as exc:
        print(f"[migration] failed without deleting legacy objects: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
