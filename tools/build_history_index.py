#!/usr/bin/env python3
"""Publish validated daily history shards from bounded R2 public projections."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:  # pragma: no cover
    boto3 = None
    BotoConfig = None


PROJECTION_RE = re.compile(r"^public-history/(\d{4}-\d{2}-\d{2})/([^/]+)\.json$")
PUBLIC_FIELDS = {
    "id", "short_id", "date", "type", "title", "source", "url", "category",
    "tags", "score", "first_seen", "last_seen", "occurrence_count", "summary",
}
FORBIDDEN_FIELDS = {
    "full_text", "raw_payload", "raw_response", "prompt", "messages", "content",
    "playbook", "significance", "observation", "source_urls", "private_config",
}
ALLOWED_TYPES = {"news", "event_cluster", "ai_digest", "hotlist", "rss"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def s3_client_from_env():
    required = ("S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("missing R2 environment values: " + ", ".join(missing))
    if boto3 is None or BotoConfig is None:
        raise RuntimeError("boto3 is required to publish history")
    endpoint_url = os.environ["S3_ENDPOINT_URL"]
    signature_version = "s3" if any(host in endpoint_url.lower() for host in ("myqcloud.com", "aliyuncs.com")) else "s3v4"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("S3_REGION") or "auto",
        config=BotoConfig(s3={"addressing_style": "virtual"}, signature_version=signature_version),
    )
    return client, os.environ["S3_BUCKET_NAME"]


def list_projection_keys(client, bucket: str, cutoff: str, limit: int = 120) -> list[tuple[str, str]]:
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="public-history/"):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            match = PROJECTION_RE.match(key)
            if match and match.group(1) >= cutoff:
                keys.append((key, match.group(1)))
    keys.sort(key=lambda item: (item[1], item[0]), reverse=True)
    if len(keys) > limit:
        raise RuntimeError(
            f"public projection count {len(keys)} exceeds bounded limit {limit}; "
            "compact producer projections before publishing"
        )
    return sorted(keys, key=lambda item: (item[1], item[0]))


def read_projection(client, bucket: str, key: str) -> dict[str, Any]:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        payload = response["Body"].read()
        data = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"failed to read projection {key}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError(f"invalid public projection shape: {key}")
    return data


def safe_http_url(value: Any) -> str:
    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return url if parts.scheme.lower() in {"http", "https"} and bool(parts.netloc) else ""


def validate_record(record: Any, key: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError(f"record is not an object in {key}")
    extra = set(record) - PUBLIC_FIELDS
    forbidden = set(record) & FORBIDDEN_FIELDS
    if extra or forbidden:
        raise ValueError(f"public record contains forbidden/unknown fields in {key}: {sorted(extra | forbidden)}")
    clean = {field: record.get(field) for field in PUBLIC_FIELDS}
    clean["id"] = str(clean.get("id") or "").strip()
    clean["title"] = str(clean.get("title") or "").strip()
    clean["date"] = str(clean.get("date") or "").strip()
    clean["type"] = str(clean.get("type") or "news").strip()
    if not clean["id"] or not clean["title"] or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean["date"]):
        raise ValueError(f"public record lacks id/title/date in {key}")
    if clean["type"] not in ALLOWED_TYPES:
        raise ValueError(f"unsupported public record type {clean['type']!r} in {key}")
    clean["url"] = safe_http_url(clean.get("url"))
    clean["summary"] = re.sub(r"\s+", " ", str(clean.get("summary") or "")).strip()[:280]
    clean["tags"] = [str(tag)[:80] for tag in (clean.get("tags") or []) if str(tag).strip()][:10]
    clean["score"] = max(0, min(100, int(clean.get("score") or 0)))
    clean["occurrence_count"] = max(1, int(clean.get("occurrence_count") or 1))
    for field in ("short_id", "source", "category", "first_seen", "last_seen"):
        clean[field] = str(clean.get(field) or "").strip()
    return clean


def _earliest(left: str, right: str) -> str:
    values = [value for value in (left, right) if value]
    return min(values) if values else ""


def _latest(left: str, right: str) -> str:
    values = [value for value in (left, right) if value]
    return max(values) if values else ""


def merge_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in sorted(records, key=lambda item: (item["date"], item.get("last_seen", ""))):
        key = record["id"] or record["url"] or f"{record['type']}|{record['source']}|{record['title'].lower()}"
        current = merged.get(key)
        if current is None:
            merged[key] = dict(record)
            continue
        first_seen = _earliest(current.get("first_seen", ""), record.get("first_seen", ""))
        last_seen = _latest(current.get("last_seen", ""), record.get("last_seen", ""))
        occurrence_count = int(current.get("occurrence_count") or 1) + int(record.get("occurrence_count") or 1)
        tags = list(dict.fromkeys((current.get("tags") or []) + (record.get("tags") or [])))[:10]
        if (record["date"], record.get("last_seen", "")) >= (current["date"], current.get("last_seen", "")):
            current = dict(record)
        current.update({
            "first_seen": first_seen,
            "last_seen": last_seen,
            "occurrence_count": occurrence_count,
            "tags": tags,
        })
        merged[key] = current
    return sorted(
        merged.values(),
        key=lambda item: (item.get("date", ""), item.get("last_seen", ""), item.get("score", 0)),
        reverse=True,
    )


def write_site(output_dir: Path, records: list[dict[str, Any]], retention_days: int) -> Path:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["date"], []).append(record)
    generated_at = utc_now().isoformat(timespec="seconds")
    staging = output_dir.parent / f".{output_dir.name}-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        days_dir = staging / "days"
        days_dir.mkdir(parents=True)
        day_entries = []
        for date in sorted(grouped, reverse=True):
            items = grouped[date]
            payload = {"date": date, "total": len(items), "items": items}
            (days_dir / f"{date}.json").write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            day_entries.append({"date": date, "count": len(items), "path": f"days/{date}.json"})
        manifest = {
            "schema_version": 1,
            "generated_at": generated_at,
            "retention_days": retention_days,
            "total": len(records),
            "days": day_entries,
            "types": sorted({record["type"] for record in records}),
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        if manifest_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("history manifest exceeds 2 MB")

        output_dir.mkdir(parents=True, exist_ok=True)
        target_days = output_dir / "days"
        target_days.mkdir(parents=True, exist_ok=True)
        expected = {f"{date}.json" for date in grouped}
        for stale in target_days.glob("*.json"):
            if stale.name not in expected:
                stale.unlink()
        for source in days_dir.glob("*.json"):
            os.replace(source, target_days / source.name)
        os.replace(manifest_path, output_dir / "manifest.json")
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output_dir / "manifest.json"


def upload_site(client, bucket: str, output_dir: Path, manifest_key: str) -> None:
    base = manifest_key.rsplit("/", 1)[0] if "/" in manifest_key else "history"
    files = [*sorted((output_dir / "days").glob("*.json")), output_dir / "manifest.json"]
    for path in files:
        key = manifest_key if path.name == "manifest.json" else f"{base}/days/{path.name}"
        body = path.read_bytes()
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentLength=len(body),
            ContentType="application/json; charset=utf-8",
            CacheControl="public, max-age=300",
        )
    print(f"[history] uploaded manifest and {len(files) - 1} daily shards")


def build_index(retention_days: int, output_dir: Path, upload_key: str = "", projection_limit: int = 120) -> int:
    client, bucket = s3_client_from_env()
    cutoff = (utc_now() - timedelta(days=retention_days - 1)).date().isoformat()
    keys = list_projection_keys(client, bucket, cutoff, limit=projection_limit)
    if not keys:
        raise RuntimeError("R2 has no public history projections; refusing to replace the current Pages site")
    records = []
    for key, projection_date in keys:
        projection = read_projection(client, bucket, key)
        for raw_record in projection["records"]:
            record = validate_record(raw_record, key)
            if record["date"] != projection_date:
                raise ValueError(f"record date differs from projection path: {key}")
            records.append(record)
    merged = merge_records(records)
    if not merged:
        raise RuntimeError("all public projections were empty; refusing to publish an empty site")
    manifest_path = write_site(output_dir, merged, retention_days)
    if upload_key:
        upload_site(client, bucket, output_dir, upload_key)
    print(
        f"[history] projections={len(keys)} input_records={len(records)} "
        f"published_records={len(merged)} manifest={manifest_path}"
    )
    return len(merged)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Ravenis Core sharded public history")
    parser.add_argument("--retention-days", type=int, default=int(os.environ.get("HISTORY_RETENTION_DAYS", "30")))
    parser.add_argument("--output-dir", default="docs/history")
    parser.add_argument("--upload-key", default=os.environ.get("HISTORY_INDEX_KEY", "history/manifest.json"))
    parser.add_argument("--projection-limit", type=int, default=120)
    parser.add_argument("--prune-remote", action="store_true", help="Deprecated no-op; cleanup requires a separate audited migration")
    args = parser.parse_args()
    if args.prune_remote:
        print("[history] prune skipped: remote deletion is isolated from publishing")
    try:
        build_index(
            retention_days=max(args.retention_days, 1),
            output_dir=Path(args.output_dir),
            upload_key=args.upload_key,
            projection_limit=max(args.projection_limit, 1),
        )
        return 0
    except Exception as exc:
        print(f"[history] publish failed; previous Pages version must be retained: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
