#!/usr/bin/env python3
"""Rebuild the last 30 days of public projections with scoring schema v3.

The command is dry-run by default.  ``--apply`` backs up every existing public
projection by content hash, writes the replacement, and verifies the uploaded
SHA-256.  Private run packages and legacy objects are never modified or deleted.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_history_index import s3_client_from_env, safe_http_url, utc_now
from trendradar.intelligence import build_public_projection
from trendradar.ranking import ScoringContext, perspective_for_slot, ranking_sort_key, score_record

RUN_KEY_RE = re.compile(
    r"^runs/(\d{4})/(\d{2})/(\d{2})/(A|B|C|DIGEST)/([^/]+)\.json\.gz$"
)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_rules() -> dict[str, Any]:
    public_path = ROOT / "config" / "intelligence_rules.yaml"
    rules = yaml.safe_load(public_path.read_text(encoding="utf-8")) or {}
    private_dir = os.environ.get("RAVENIS_PRIVATE_CONFIG_DIR", "").strip()
    if private_dir:
        private_path = Path(private_dir) / "intelligence_rules.yaml"
        if private_path.exists():
            rules = deep_merge(
                rules,
                yaml.safe_load(private_path.read_text(encoding="utf-8")) or {},
            )
    return rules


def list_recent_runs(client, bucket: str, retention_days: int) -> list[str]:
    cutoff = (utc_now() - timedelta(days=retention_days - 1)).date().isoformat()
    keys = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="runs/"):
        for item in page.get("Contents", []):
            key = str(item.get("Key") or "")
            match = RUN_KEY_RE.fullmatch(key)
            if not match:
                continue
            date = "-".join(match.group(index) for index in (1, 2, 3))
            if date >= cutoff:
                keys.append(key)
    slot_order = {"A": 0, "DIGEST": 1, "B": 2, "C": 3}

    def order(key: str) -> tuple[str, int, str]:
        match = RUN_KEY_RE.fullmatch(key)
        if not match:
            return "", 99, key
        date = "-".join(match.group(index) for index in (1, 2, 3))
        return date, slot_order.get(match.group(4), 99), key

    return sorted(keys, key=order)


def decode_run(payload: bytes) -> dict[str, Any]:
    if payload.startswith(b"\x1f\x8b"):
        payload = gzip.decompress(payload)
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("private run package root is not an object")
    return value


def _run_now(value: dict[str, Any], date: str) -> datetime:
    raw = str(value.get("generated_at") or f"{date}T12:00:00")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.fromisoformat(f"{date}T12:00:00")


def rescore_crawler_run(
    value: dict[str, Any],
    rules: dict[str, Any],
    recent_records: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    package = deepcopy(value)
    date = str(package.get("date") or "")
    slot = str(package.get("slot") or "").upper()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or slot not in {"A", "B", "C"}:
        raise ValueError("crawler run lacks a valid date/slot")
    perspective = perspective_for_slot(slot, rules)
    context = ScoringContext(
        rules=rules,
        now=_run_now(package, date),
        recent_records=recent_records,
        history_status="ok",
    )
    items = [deepcopy(item) for item in package.get("raw_items", []) if isinstance(item, dict)]
    category_ids = {
        str(item.get("name") or ""): str(item.get("id") or "")
        for item in rules.get("categories", []) or []
        if isinstance(item, dict)
    }
    for item in items:
        if not str(item.get("id") or "").strip():
            raise ValueError("crawler run contains a record without a stable ID")
        if not item.get("category_id"):
            item["category_id"] = category_ids.get(str(item.get("category") or ""), "other")
        result = score_record(item, perspective, context)
        item["raw_item_score"] = result.score
        item["score_components"] = dict(result.components)
        item["rank_reasons"] = list(result.reasons)
        item["rank_penalties"] = list(result.penalties)
        item["rank_perspective"] = perspective
    items.sort(key=ranking_sort_key)
    item_by_id = {str(item["id"]): item for item in items}
    clusters = [deepcopy(cluster) for cluster in package.get("clusters", []) if isinstance(cluster, dict)]
    for cluster in clusters:
        members = [
            item_by_id.get(str(item_id))
            for item_id in cluster.get("related_item_ids", []) or []
        ]
        members = [member for member in members if member]
        if not members:
            continue
        source_count = max(1, int(cluster.get("source_count") or 1))
        cluster["cluster_score"] = min(
            100,
            max(int(member.get("raw_item_score") or 0) for member in members)
            + min(15, max(0, source_count - 1) * 5),
        )
    package.update({
        "schema_version": 3,
        "perspective": perspective,
        "raw_items": items,
        "clusters": clusters,
        "ranking_status": "ok",
    })
    return build_public_projection(package), items


def _domain_sources(topic: dict[str, Any]) -> list[dict[str, str]]:
    values = []
    seen = set()
    for url in topic.get("source_urls", []) or []:
        try:
            domain = (urlsplit(str(url)).hostname or "").lower().removeprefix("www.")
        except ValueError:
            continue
        if not domain or domain in seen:
            continue
        seen.add(domain)
        values.append({
            "source": domain,
            "source_id": domain,
            "publisher_key": domain,
            "source_type": "external",
            "url": safe_http_url(url),
        })
    return values


def rescore_digest_run(
    value: dict[str, Any],
    rules: dict[str, Any],
    recent_records: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    date = str(value.get("date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("AI Digest run lacks a valid date")
    generated_at = str(value.get("generated_at") or f"{date}T10:00:00")
    analysis = value.get("analysis") or {}
    analysis_items = analysis.get("items") or {}
    context = ScoringContext(rules, _run_now(value, date), recent_records, "ok")
    scored = []
    public_by_id = {}
    for topic in value.get("topics", []) or []:
        if not isinstance(topic, dict):
            continue
        item_id = str(topic.get("digest_id") or "").strip()
        title = str(topic.get("title") or "").strip()
        if not item_id or not title:
            continue
        sources = _domain_sources(topic)
        published_at = str(topic.get("published_at") or f"{date}T10:00:00")
        record = {
            "id": item_id,
            "title": title,
            "summary": " ".join(
                str(topic.get(field) or "").strip()
                for field in ("sentence", "significance")
                if str(topic.get(field) or "").strip()
            ),
            "category_id": "ai_models",
            "category": "AI / 模型",
            "tags": [],
            "source": "AI Digest Daily",
            "source_id": "ai-digest-daily",
            "source_type": "curated",
            "source_count": max(1, len(sources)),
            "sources": sources or [{
                "source": "AI Digest Daily", "source_id": "ai-digest-daily",
                "publisher_key": "ai-digest-daily", "source_type": "curated",
                "url": safe_http_url(topic.get("page_url")),
            }],
            "url": safe_http_url(topic.get("primary_url") or topic.get("page_url")),
            "captured_at": published_at,
            "first_seen": published_at,
            "last_seen": published_at,
            "occurrence_count": 1,
            "platform_rank": 0,
            "intent": ["WORK_CORE"],
        }
        result = score_record(record, "A", context)
        record.update({
            "raw_item_score": result.score,
            "score_components": dict(result.components),
            "rank_reasons": list(result.reasons),
        })
        scored.append(record)
        analysis_row = analysis_items.get(item_id, {}) if isinstance(analysis_items, dict) else {}
        summary = str(analysis_row.get("summary") or record["summary"])[:280]
        public_by_id[item_id] = {
            "id": item_id,
            "short_id": f"D{int(topic.get('item_index') or 0):03d}",
            "date": date,
            "type": "ai_digest",
            "title": title,
            "source": "AI Digest Daily",
            "source_count": record["source_count"],
            "url": record["url"],
            "category": "AI / 模型",
            "tags": list(analysis_row.get("tags") or [])[:10],
            "score": result.score,
            "first_seen": published_at,
            "last_seen": published_at,
            "occurrence_count": 1,
            "summary": summary,
        }
    scored.sort(key=ranking_sort_key)
    ranking = [
        {"id": item["id"], "score": item["raw_item_score"], "reasons": item["rank_reasons"][:2]}
        for item in scored
    ]
    projection = {
        "schema_version": 3,
        "date": date,
        "generated_at": generated_at,
        "run": {
            "slot": "DIGEST",
            "perspective": "A",
            "source": "ai-digest",
            "generated_at": generated_at,
            "record_ids": [item["id"] for item in ranking],
            "ranking": ranking,
            "summary": {},
        },
        "records": [public_by_id[item["id"]] for item in ranking],
    }
    return projection, scored


def rescore_run(
    value: dict[str, Any],
    rules: dict[str, Any],
    recent_records: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    if isinstance(value.get("raw_items"), list):
        projection, scored = rescore_crawler_run(value, rules, recent_records)
        return projection, scored, "ravenis"
    if isinstance(value.get("topics"), list):
        projection, scored = rescore_digest_run(value, rules, recent_records)
        return projection, scored, "ai-digest"
    raise ValueError("unsupported private run package shape")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def migrate(retention_days: int, *, apply: bool = False, max_runs: int = 500) -> dict[str, Any]:
    client, bucket = s3_client_from_env()
    rules = load_rules()
    keys = list_recent_runs(client, bucket, retention_days)
    if len(keys) > max_runs:
        raise RuntimeError(f"refusing to process {len(keys)} runs above --max-runs={max_runs}")
    report = {
        "generated_at": utc_now().isoformat(timespec="seconds"),
        "mode": "apply" if apply else "dry-run",
        "retention_days": retention_days,
        "runs": [],
    }
    recent_records: dict[str, dict[str, Any]] = {}
    backed_up = set()
    for key in keys:
        payload = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        private_run = decode_run(payload)
        projection, scored, source = rescore_run(private_run, rules, recent_records)
        ids = [str(item.get("id") or "") for item in projection["records"]]
        ranking_ids = projection["run"]["record_ids"]
        if not ids or set(ids) != set(ranking_ids) or len(ranking_ids) != len(set(ranking_ids)):
            raise RuntimeError(f"projection count/ID verification failed for {key}")
        date = projection["date"]
        slot = projection["run"]["slot"]
        target = f"public-history/{date}/{source}-{slot}.json"
        body = _json_bytes(projection)
        digest = hashlib.sha256(body).hexdigest()
        backup = ""
        if apply:
            if target not in backed_up:
                try:
                    old_body = client.get_object(Bucket=bucket, Key=target)["Body"].read()
                except Exception:
                    old_body = b""
                if old_body:
                    old_digest = hashlib.sha256(old_body).hexdigest()
                    backup = f"history/backups/scoring-v2/{date}/{source}-{slot}-{old_digest[:12]}.json"
                    client.put_object(
                        Bucket=bucket,
                        Key=backup,
                        Body=old_body,
                        ContentLength=len(old_body),
                        ContentType="application/json; charset=utf-8",
                    )
                backed_up.add(target)
            client.put_object(
                Bucket=bucket,
                Key=target,
                Body=body,
                ContentLength=len(body),
                ContentType="application/json; charset=utf-8",
            )
            remote = client.get_object(Bucket=bucket, Key=target)["Body"].read()
            if hashlib.sha256(remote).hexdigest() != digest:
                raise RuntimeError(f"SHA-256 verification failed after upload: {target}")
        report["runs"].append({
            "private_key": key,
            "target": target,
            "backup": backup,
            "records": len(ids),
            "sha256": digest,
        })
        for record in scored:
            recent_records[str(record["id"])] = deepcopy(record)
        print(f"[rescore] mode={report['mode']} target={target} records={len(ids)} sha256={digest}")
    report["run_count"] = len(report["runs"])
    report["record_count"] = sum(item["records"] for item in report["runs"])
    output = ROOT / "output" / "rescore-history-v3-report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[rescore] runs={report['run_count']} records={report['record_count']} report={output}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--max-runs", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = migrate(
            max(1, min(args.retention_days, 31)),
            apply=args.apply,
            max_runs=max(1, args.max_runs),
        )
        return 0 if report["run_count"] else 1
    except Exception as exc:
        print(f"[rescore] failed without deleting private runs: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
