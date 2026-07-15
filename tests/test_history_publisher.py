from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tools.build_history_index as publisher
from tools.build_history_index import merge_records, validate_record, write_site


def public_record(**override):
    record = {
        "id": "r_1", "short_id": "A001", "date": "2026-07-15", "type": "news",
        "title": "Example", "source": "Source", "url": "https://example.com/a",
        "category": "AI / 模型", "tags": ["AI"], "score": 80,
        "first_seen": "2026-07-14T01:00:00", "last_seen": "2026-07-15T01:00:00",
        "occurrence_count": 1, "summary": "short",
    }
    record.update(override)
    return record


def test_latest_record_wins_but_first_seen_is_preserved():
    old = public_record(date="2026-07-14", title="Old", last_seen="2026-07-14T02:00:00")
    new = public_record(date="2026-07-15", title="New", first_seen="2026-07-15T01:00:00")
    merged = merge_records([old, new])[0]
    assert merged["title"] == "New"
    assert merged["first_seen"] == "2026-07-14T01:00:00"
    assert merged["last_seen"] == "2026-07-15T01:00:00"
    assert merged["occurrence_count"] == 2


def test_forbidden_public_fields_fail_closed():
    with pytest.raises(ValueError):
        validate_record(public_record(full_text="secret"), "projection.json")
    assert validate_record(public_record(url="javascript:alert(1)"), "projection.json")["url"] == ""


def test_daily_shards_and_small_manifest():
    output = Path("tests/_runtime_history")
    write_site(output, [public_record()], retention_days=30)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["days"][0]["path"] == "days/2026-07-15.json"
    assert (output / "days" / "2026-07-15.json").exists()
    assert (output / "manifest.json").stat().st_size < 2 * 1024 * 1024


class FakeBody:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload


class FakePaginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, **_kwargs):
        return [{"Contents": [{"Key": key} for key in self.keys]}]


class FakeProjectionS3:
    def __init__(self, projections):
        self.projections = projections

    def get_paginator(self, _name):
        return FakePaginator(list(self.projections))

    def get_object(self, Bucket, Key):
        del Bucket
        return {"Body": FakeBody(json.dumps(self.projections[Key]).encode("utf-8"))}


def test_120_projection_publish_is_bounded(monkeypatch):
    projections = {}
    for day in range(1, 31):
        date = f"2026-06-{day:02d}"
        for slot in ("A", "B", "C", "DIGEST"):
            key = f"public-history/{date}/source-{slot}.json"
            projections[key] = {
                "records": [public_record(id=f"{date}-{slot}", date=date)]
            }
    fake = FakeProjectionS3(projections)
    monkeypatch.setattr(publisher, "s3_client_from_env", lambda: (fake, "bucket"))
    monkeypatch.setattr(
        publisher,
        "utc_now",
        lambda: datetime(2026, 6, 30, tzinfo=timezone.utc),
    )
    output = Path("tests/_runtime_history_many")
    started = time.monotonic()
    assert publisher.build_index(30, output, upload_key="", projection_limit=120) == 120
    assert time.monotonic() - started < 2
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["days"]) == 30


def test_empty_r2_does_not_replace_previous_manifest(monkeypatch):
    output = Path("tests/_runtime_history")
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "manifest.json"
    manifest.write_text("previous", encoding="utf-8")
    fake = FakeProjectionS3({})
    monkeypatch.setattr(publisher, "s3_client_from_env", lambda: (fake, "bucket"))
    with pytest.raises(RuntimeError):
        publisher.build_index(30, output, upload_key="")
    assert manifest.read_text(encoding="utf-8") == "previous"
