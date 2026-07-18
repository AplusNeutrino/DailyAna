#!/usr/bin/env python3
"""Publish validated daily history shards from bounded R2 public projections."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trendradar.weekly import build_weekly_digest

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:  # pragma: no cover
    boto3 = None
    BotoConfig = None


PROJECTION_RE = re.compile(r"^public-history/(\d{4}-\d{2}-\d{2})/([^/]+)\.json$")
PUBLIC_FIELDS = {
    "id", "short_id", "date", "type", "title", "source", "source_count", "url", "category",
    "tags", "score", "first_seen", "last_seen", "occurrence_count", "summary",
}
FORBIDDEN_FIELDS = {
    "full_text", "raw_payload", "raw_response", "prompt", "messages", "content",
    "playbook", "significance", "observation", "source_urls", "private_config",
}
ALLOWED_TYPES = {"news", "event_cluster", "ai_digest", "hotlist", "rss"}
RUN_FIELDS = {"slot", "perspective", "source", "generated_at", "record_ids", "ranking", "summary"}
RANKING_FIELDS = {"id", "score", "reasons"}
SUMMARY_FIELDS = {"status", "overview", "top_items", "watchlist"}
TOP_ITEM_FIELDS = {"id", "headline", "event", "impact", "watch", "evidence_ids"}
WATCH_ITEM_FIELDS = {"text", "evidence_ids"}
RELEASE_STATIC_FILES = ("manifest.json", "search-index.json", "weekly/latest.json")


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
    clean = {field: record.get(field) for field in sorted(PUBLIC_FIELDS)}
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
    clean["source_count"] = max(1, int(clean.get("source_count") or 1))
    clean["occurrence_count"] = max(1, int(clean.get("occurrence_count") or 1))
    for field in ("short_id", "source", "category", "first_seen", "last_seen"):
        clean[field] = str(clean.get(field) or "").strip()
    return clean


def is_valid_event_cluster(record: dict[str, Any]) -> bool:
    """Keep only clusters backed by at least two records and two sources."""
    if record.get("type") != "event_cluster":
        return True
    source_count = int(record.get("source_count") or 0)
    member_count = int(record.get("occurrence_count") or 0)
    source_label = str(record.get("source") or "").strip()
    contradicts_count = bool(re.match(r"^[01]\s*个(?:独立|公开)?来源", source_label))
    return source_count >= 2 and member_count >= 2 and not contradicts_count


def partition_publishable_records(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    publishable = [record for record in records if is_valid_event_cluster(record)]
    return publishable, len(records) - len(publishable)


def filter_runs_to_records(
    runs: list[dict[str, Any]],
    record_ids: set[str],
) -> list[dict[str, Any]]:
    """Remove skipped records and their summary references without failing old releases."""
    filtered = []
    for run in runs:
        clean = dict(run)
        clean["record_ids"] = [value for value in run.get("record_ids", []) if value in record_ids]
        clean["ranking"] = [
            item for item in run.get("ranking", []) or []
            if item.get("id") in record_ids
        ]
        if clean["ranking"]:
            ranked_ids = [item["id"] for item in clean["ranking"]]
            clean["record_ids"] = ranked_ids
        if not clean["record_ids"]:
            continue
        summary = dict(run.get("summary") or {})
        summary["top_items"] = [
            item for item in summary.get("top_items", [])
            if item.get("id") in record_ids
            and all(value in record_ids for value in item.get("evidence_ids", []))
        ]
        summary["watchlist"] = [
            item for item in summary.get("watchlist", [])
            if item.get("evidence_ids")
            and all(value in record_ids for value in item.get("evidence_ids", []))
        ]
        clean["summary"] = summary
        filtered.append(clean)
    return filtered


def _compact_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _projection_identity(key: str) -> tuple[str, str]:
    stem = Path(key).stem
    if "-" not in stem:
        return stem, ""
    source, slot = stem.rsplit("-", 1)
    return source, slot.upper()


def validate_run(
    run: Any,
    key: str,
    date: str,
    record_ids: set[str],
) -> dict[str, Any]:
    source_from_key, slot_from_key = _projection_identity(key)
    if run is None:
        run = {}
    if not isinstance(run, dict) or set(run) - RUN_FIELDS:
        raise ValueError(f"public run metadata has an invalid shape in {key}")
    slot = _compact_text(run.get("slot") or slot_from_key, 24).upper()
    source = _compact_text(run.get("source") or source_from_key, 40)
    generated_at = _compact_text(run.get("generated_at"), 40)
    perspective = _compact_text(run.get("perspective"), 1).upper()
    if not perspective:
        perspective = "A" if slot in {"A", "DIGEST"} else slot if slot in {"B", "C"} else ""
    if perspective and perspective not in {"A", "B", "C"}:
        raise ValueError(f"public run has an invalid perspective in {key}")
    run_record_ids = [
        _compact_text(value, 80)
        for value in run.get("record_ids", []) or []
        if _compact_text(value, 80) in record_ids
    ]
    if not run_record_ids:
        run_record_ids = sorted(record_ids)
    ranking = []
    seen_ranking_ids: set[str] = set()
    for raw_item in run.get("ranking", []) or []:
        if not isinstance(raw_item, dict) or set(raw_item) - RANKING_FIELDS:
            raise ValueError(f"public run ranking has an invalid shape in {key}")
        item_id = _compact_text(raw_item.get("id"), 80)
        if item_id not in record_ids or item_id in seen_ranking_ids:
            continue
        reasons = [
            _compact_text(value, 80)
            for value in raw_item.get("reasons", []) or []
            if _compact_text(value, 80)
        ][:2]
        ranking.append({
            "id": item_id,
            "score": max(0, min(100, int(raw_item.get("score") or 0))),
            "reasons": reasons,
        })
        seen_ranking_ids.add(item_id)
    if ranking:
        ranked_ids = [item["id"] for item in ranking]
        run_record_ids = ranked_ids
    raw_summary = run.get("summary") or {}
    if not isinstance(raw_summary, dict) or set(raw_summary) - SUMMARY_FIELDS:
        raise ValueError(f"public digest summary has an invalid shape in {key}")
    status = "ai" if raw_summary.get("status") == "ai" else "rules"
    top_items = []
    for raw_item in raw_summary.get("top_items", []) or []:
        if not isinstance(raw_item, dict) or set(raw_item) - TOP_ITEM_FIELDS:
            raise ValueError(f"public digest top item has an invalid shape in {key}")
        item_id = _compact_text(raw_item.get("id"), 80)
        evidence_ids = [
            _compact_text(value, 80)
            for value in raw_item.get("evidence_ids", []) or []
            if _compact_text(value, 80) in record_ids
        ][:6]
        if item_id not in record_ids or item_id not in evidence_ids:
            raise ValueError(f"public digest top item references unknown evidence in {key}")
        top_items.append({
            "id": item_id,
            "headline": _compact_text(raw_item.get("headline"), 24),
            "event": _compact_text(raw_item.get("event"), 56),
            "impact": _compact_text(raw_item.get("impact"), 56),
            "watch": _compact_text(raw_item.get("watch"), 40),
            "evidence_ids": evidence_ids,
        })
    watchlist = []
    for raw_item in raw_summary.get("watchlist", []) or []:
        if not isinstance(raw_item, dict) or set(raw_item) - WATCH_ITEM_FIELDS:
            raise ValueError(f"public digest watch item has an invalid shape in {key}")
        evidence_ids = [
            _compact_text(value, 80)
            for value in raw_item.get("evidence_ids", []) or []
            if _compact_text(value, 80) in record_ids
        ][:6]
        text = _compact_text(raw_item.get("text"), 40)
        if text and evidence_ids:
            watchlist.append({"text": text, "evidence_ids": evidence_ids})
    return {
        "date": date,
        "slot": slot,
        "perspective": perspective,
        "source": source,
        "generated_at": generated_at,
        "record_ids": list(dict.fromkeys(run_record_ids)),
        "ranking": ranking,
        "summary": {
            "status": status,
            "overview": _compact_text(raw_summary.get("overview"), 80),
            "top_items": top_items[:3],
            "watchlist": watchlist[:3],
        },
    }


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


def build_search_index(records: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the lazy-loaded 30-day browser index from public fields only."""
    slots_by_id: dict[str, set[str]] = {}
    perspectives_by_id: dict[str, dict[str, dict[str, Any]]] = {}
    for run in runs:
        slot = _compact_text(run.get("slot"), 24).upper()
        if not slot:
            continue
        for record_id in run.get("record_ids", []) or []:
            slots_by_id.setdefault(str(record_id), set()).add(slot)
        perspective = _compact_text(run.get("perspective"), 1).upper()
        if perspective not in {"A", "B", "C"}:
            perspective = "A" if slot in {"A", "DIGEST"} else slot if slot in {"B", "C"} else ""
        for ranking in run.get("ranking", []) or []:
            record_id = str(ranking.get("id") or "")
            if not record_id or not perspective:
                continue
            candidate = {
                "score": max(0, min(100, int(ranking.get("score") or 0))),
                "reasons": [_compact_text(value, 80) for value in ranking.get("reasons", []) or []][:2],
            }
            current = perspectives_by_id.setdefault(record_id, {}).get(perspective)
            if current is None or candidate["score"] > current["score"]:
                perspectives_by_id[record_id][perspective] = candidate
    items = []
    for record in records:
        item = {field: record.get(field) for field in sorted(PUBLIC_FIELDS)}
        item["slots"] = sorted(slots_by_id.get(record["id"], set()))
        item["perspectives"] = perspectives_by_id.get(record["id"], {})
        items.append(item)
    return {
        "schema_version": 2,
        "generated_at": utc_now().isoformat(timespec="seconds"),
        "total": len(items),
        "items": items,
    }


def release_file_names(output_dir: Path) -> list[str]:
    names = [*RELEASE_STATIC_FILES]
    names.extend(
        f"days/{path.name}" for path in sorted((output_dir / "days").glob("*.json"))
    )
    missing = [name for name in RELEASE_STATIC_FILES if not (output_dir / name).is_file()]
    if missing:
        raise ValueError(f"history release lacks required files: {', '.join(missing)}")
    return names


def create_release_archive(output_dir: Path) -> tuple[bytes, str, list[str]]:
    """Create a deterministic, rootless tar.gz containing public data only."""
    names = release_file_names(output_dir)
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gzip_file:
        with tarfile.open(fileobj=gzip_file, mode="w") as archive:
            for name in names:
                data = (output_dir / name).read_bytes()
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(data))
    payload = compressed.getvalue()
    return payload, hashlib.sha256(payload).hexdigest(), names


def publish_release(
    client,
    bucket: str,
    output_dir: Path,
    release_prefix: str,
    pointer_key: str,
    retention_days: int,
) -> dict[str, Any]:
    payload, digest, names = create_release_archive(output_dir)
    generated_at = utc_now().isoformat(timespec="seconds")
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{timestamp}-{digest[:12]}"
    object_key = f"{release_prefix.strip('/')}/{run_id}.tar.gz"
    client.put_object(
        Bucket=bucket,
        Key=object_key,
        Body=payload,
        ContentLength=len(payload),
        ContentType="application/gzip",
        CacheControl="public, max-age=31536000, immutable",
        Metadata={"sha256": digest},
    )
    pointer = {
        "schema_version": 1,
        "run_id": run_id,
        "generated_at": generated_at,
        "object_key": object_key,
        "sha256": digest,
        "size": len(payload),
        "retention_days": retention_days,
        "files": names,
    }
    pointer_body = json.dumps(pointer, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # The mutable pointer is deliberately written last. Consumers either see the
    # previous complete release or this complete release, never an in-between set.
    client.put_object(
        Bucket=bucket,
        Key=pointer_key,
        Body=pointer_body,
        ContentLength=len(pointer_body),
        ContentType="application/json; charset=utf-8",
        CacheControl="no-store",
    )
    print(f"[history] release={object_key} sha256={digest} pointer={pointer_key}")
    return pointer


def write_site(
    output_dir: Path,
    records: list[dict[str, Any]],
    retention_days: int,
    runs: list[dict[str, Any]] | None = None,
    weekly_records: list[dict[str, Any]] | None = None,
) -> Path:
    records, skipped_invalid_clusters = partition_publishable_records(records)
    if not records:
        raise RuntimeError("no publishable public records; refusing to replace the current site")
    runs = filter_runs_to_records(runs or [], {record["id"] for record in records})
    weekly_records, weekly_skipped = partition_publishable_records(weekly_records or records)
    skipped_invalid_clusters = max(skipped_invalid_clusters, weekly_skipped)
    if skipped_invalid_clusters:
        print(f"[history] skipped_invalid_clusters={skipped_invalid_clusters}")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["date"], []).append(record)
    runs_by_date: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        runs_by_date.setdefault(run["date"], []).append(run)
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
            day_runs = sorted(
                runs_by_date.get(date, []),
                key=lambda run: (run.get("slot", ""), run.get("generated_at", "")),
            )
            payload = {"date": date, "total": len(items), "runs": day_runs, "items": items}
            (days_dir / f"{date}.json").write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
            )
            day_entries.append({"date": date, "count": len(items), "path": f"days/{date}.json"})
        weekly_payload = build_weekly_digest(
            weekly_records,
            end_date=max(grouped),
            window_days=7,
            max_themes=12,
        )
        weekly_dir = staging / "weekly"
        weekly_dir.mkdir(parents=True)
        weekly_path = weekly_dir / "latest.json"
        weekly_path.write_text(
            json.dumps(weekly_payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        search_payload = build_search_index(records, runs)
        search_path = staging / "search-index.json"
        search_path.write_text(
            json.dumps(search_payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        search_sha256 = hashlib.sha256(search_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": 3,
            "generated_at": generated_at,
            "retention_days": retention_days,
            "total": len(records),
            "run_count": sum(len(value) for value in runs_by_date.values()),
            "days": day_entries,
            "types": sorted({record["type"] for record in records}),
            "slots": sorted({run.get("slot", "") for run in runs if run.get("slot")}),
            "weekly": {
                "path": "weekly/latest.json",
                "start_date": weekly_payload["start_date"],
                "end_date": weekly_payload["end_date"],
                "record_count": weekly_payload["record_count"],
                "theme_count": weekly_payload["theme_count"],
            },
            "search": {
                "path": "search-index.json",
                "count": len(records),
                "sha256": search_sha256,
            },
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
        target_weekly = output_dir / "weekly"
        target_weekly.mkdir(parents=True, exist_ok=True)
        os.replace(weekly_path, target_weekly / weekly_path.name)
        os.replace(search_path, output_dir / "search-index.json")
        os.replace(manifest_path, output_dir / "manifest.json")
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output_dir / "manifest.json"


def upload_site(client, bucket: str, output_dir: Path, manifest_key: str) -> None:
    base = manifest_key.rsplit("/", 1)[0] if "/" in manifest_key else "history"
    files = [
        *sorted((output_dir / "days").glob("*.json")),
        *sorted((output_dir / "weekly").glob("*.json")),
        output_dir / "search-index.json",
        output_dir / "manifest.json",
    ]
    for path in files:
        if path.name == "manifest.json":
            key = manifest_key
        elif path.name == "search-index.json":
            key = f"{base}/search-index.json"
        elif path.parent.name == "weekly":
            key = f"{base}/weekly/{path.name}"
        else:
            key = f"{base}/days/{path.name}"
        body = path.read_bytes()
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentLength=len(body),
            ContentType="application/json; charset=utf-8",
            CacheControl="public, max-age=300",
        )
    print(f"[history] uploaded manifest, search index, {len(files) - 3} daily shards, and one weekly digest")


def build_index(
    retention_days: int,
    output_dir: Path,
    upload_key: str = "",
    projection_limit: int = 120,
    release_prefix: str = "",
    pointer_key: str = "",
) -> int:
    client, bucket = s3_client_from_env()
    cutoff = (utc_now() - timedelta(days=retention_days - 1)).date().isoformat()
    keys = list_projection_keys(client, bucket, cutoff, limit=projection_limit)
    if not keys:
        raise RuntimeError("R2 has no public history projections; refusing to replace the current Pages site")
    records = []
    runs = []
    for key, projection_date in keys:
        projection = read_projection(client, bucket, key)
        projection_records = []
        for raw_record in projection["records"]:
            record = validate_record(raw_record, key)
            if record["date"] != projection_date:
                raise ValueError(f"record date differs from projection path: {key}")
            records.append(record)
            projection_records.append(record)
        runs.append(validate_run(
            projection.get("run"),
            key,
            projection_date,
            {record["id"] for record in projection_records},
        ))
    merged, skipped_merged_clusters = partition_publishable_records(merge_records(records))
    weekly_records, skipped_input_clusters = partition_publishable_records(records)
    if not merged:
        raise RuntimeError("all public projections were empty; refusing to publish an empty site")
    runs = filter_runs_to_records(runs, {record["id"] for record in merged})
    manifest_path = write_site(
        output_dir,
        merged,
        retention_days,
        runs=runs,
        weekly_records=weekly_records,
    )
    if upload_key:
        upload_site(client, bucket, output_dir, upload_key)
    if release_prefix or pointer_key:
        if not release_prefix or not pointer_key:
            raise ValueError("release prefix and pointer key must be configured together")
        publish_release(client, bucket, output_dir, release_prefix, pointer_key, retention_days)
    print(
        f"[history] projections={len(keys)} input_records={len(records)} "
        f"published_records={len(merged)} skipped_invalid_clusters={skipped_input_clusters} "
        f"skipped_merged_clusters={skipped_merged_clusters} manifest={manifest_path}"
    )
    return len(merged)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Ravenis Core sharded public history")
    parser.add_argument("--retention-days", type=int, default=int(os.environ.get("HISTORY_RETENTION_DAYS", "30")))
    parser.add_argument("--output-dir", default="docs/history")
    parser.add_argument("--upload-key", default=os.environ.get("HISTORY_INDEX_KEY", "history/manifest.json"))
    parser.add_argument("--projection-limit", type=int, default=120)
    parser.add_argument("--release-prefix", default=os.environ.get("HISTORY_RELEASE_PREFIX", "history/releases"))
    parser.add_argument("--pointer-key", default=os.environ.get("HISTORY_CURRENT_KEY", "history/current.json"))
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
            release_prefix=args.release_prefix,
            pointer_key=args.pointer_key,
        )
        return 0
    except Exception as exc:
        print(f"[history] publish failed; previous Pages version must be retained: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
