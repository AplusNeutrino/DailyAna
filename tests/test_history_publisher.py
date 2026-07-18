from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import tools.build_history_index as publisher
from tools.build_history_index import (
    create_release_archive,
    merge_records,
    publish_release,
    validate_record,
    validate_run,
    write_site,
)
from tools.migrate_legacy_history import extract_legacy_json


def test_history_publisher_direct_entrypoint_imports_project_package():
    result = subprocess.run(
        [sys.executable, "tools/build_history_index.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    lightweight = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import tools.build_history_index; "
            "assert 'litellm' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert lightweight.returncode == 0, lightweight.stderr


def public_record(**override):
    record = {
        "id": "r_1", "short_id": "A001", "date": "2026-07-15", "type": "news",
        "title": "Example", "source": "Source", "source_count": 1, "url": "https://example.com/a",
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
    assert manifest["weekly"]["path"] == "weekly/latest.json"
    assert manifest["search"]["path"] == "search-index.json"
    assert (output / "days" / "2026-07-15.json").exists()
    assert (output / "weekly" / "latest.json").exists()
    assert (output / "search-index.json").exists()
    assert (output / "manifest.json").stat().st_size < 2 * 1024 * 1024


def test_invalid_event_clusters_are_skipped_from_every_public_projection(capsys):
    output = Path("tests/_runtime_history")
    news = public_record(id="n_1", title="芯片新闻")
    invalid = public_record(
        id="c_invalid", type="event_cluster", title="芯片相关信号", url="",
        source="0 个独立来源", source_count=1, occurrence_count=1,
    )
    valid = public_record(
        id="c_valid", type="event_cluster", title="芯片供应链价格变化", url="",
        source="2 个独立来源", source_count=2, occurrence_count=2,
    )
    run = {
        "date": news["date"], "slot": "B", "source": "ravenis", "generated_at": news["last_seen"],
        "record_ids": [news["id"], invalid["id"], valid["id"]],
        "summary": {"status": "rules", "overview": "摘要", "top_items": [], "watchlist": []},
    }
    write_site(output, [news, invalid, valid], retention_days=30, runs=[run])
    shard = json.loads((output / "days" / f"{news['date']}.json").read_text(encoding="utf-8"))
    search = json.loads((output / "search-index.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert {item["id"] for item in shard["items"]} == {"n_1", "c_valid"}
    assert {item["id"] for item in search["items"]} == {"n_1", "c_valid"}
    assert shard["runs"][0]["record_ids"] == ["n_1", "c_valid"]
    assert manifest["total"] == 2
    assert "skipped_invalid_clusters=1" in capsys.readouterr().out


def test_legacy_migration_does_not_create_single_source_clusters():
    base = {
        "cluster_id": "c_legacy", "title": "芯片相关信号", "source_count": 1,
        "related_item_ids": ["n_1"], "category": "芯片 / 算力",
    }
    assert extract_legacy_json(base, "2026-07-15", "cluster") is None
    migrated = extract_legacy_json(
        dict(base, source_count=2, related_item_ids=["n_1", "n_2"]),
        "2026-07-15",
        "cluster",
    )
    assert migrated is not None
    assert migrated["source_count"] == 2
    assert migrated["occurrence_count"] == 2


def test_daily_shard_preserves_strict_run_summary():
    output = Path("tests/_runtime_history")
    record = public_record()
    run = validate_run({
        "slot": "A",
        "source": "ravenis",
        "generated_at": "2026-07-15T06:00:00",
        "record_ids": [record["id"]],
        "summary": {
            "status": "ai",
            "overview": "今天先看模型发布。",
            "top_items": [{
                "id": record["id"], "headline": "模型发布", "event": "发布新模型",
                "impact": "影响开发者选择", "watch": "观察价格与采用情况",
                "evidence_ids": [record["id"]],
            }],
            "watchlist": [{"text": "观察正式价格", "evidence_ids": [record["id"]]}],
        },
    }, "public-history/2026-07-15/ravenis-A.json", "2026-07-15", {record["id"]})
    write_site(output, [record], retention_days=30, runs=[run])
    shard = json.loads((output / "days" / "2026-07-15.json").read_text(encoding="utf-8"))
    search = json.loads((output / "search-index.json").read_text(encoding="utf-8"))
    assert shard["runs"][0]["slot"] == "A"
    assert shard["runs"][0]["summary"]["top_items"][0]["id"] == record["id"]
    assert search["items"][0]["slots"] == ["A"]


def test_public_run_summary_rejects_unknown_evidence():
    with pytest.raises(ValueError):
        validate_run({
            "slot": "B", "source": "ravenis", "record_ids": ["r_1"],
            "summary": {
                "overview": "摘要",
                "top_items": [{
                    "id": "r_missing", "headline": "标题", "event": "事件", "impact": "影响",
                    "watch": "观察价格", "evidence_ids": ["r_missing"],
                }],
            },
        }, "public-history/2026-07-15/ravenis-B.json", "2026-07-15", {"r_1"})


def test_legacy_history_page_redirects_without_losing_deep_link_state():
    page = Path("docs/history/index.html")
    text = page.read_text(encoding="utf-8")
    assert "https://neutriverse.uk/ravenis/" in text
    assert "target.search = window.location.search" in text
    assert "target.hash = window.location.hash" in text
    assert 'content="noindex,nofollow"' in text


def test_release_archive_contains_only_public_data_files():
    output = Path("tests/_runtime_history_release")
    write_site(output, [public_record()], retention_days=30)
    payload, digest, names = create_release_archive(output)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert set(names) == {
        "manifest.json", "search-index.json", "weekly/latest.json", "days/2026-07-15.json",
    }
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        archive_names = archive.getnames()
        assert archive_names == names
        assert all(not name.startswith(("/", "../")) and "/../" not in name for name in archive_names)
        assert not any("sqlite" in name or "prompt" in name for name in archive_names)


class FakeWriteS3:
    def __init__(self):
        self.writes = []

    def put_object(self, **kwargs):
        self.writes.append(kwargs)


def test_release_pointer_is_written_last_and_matches_archive(monkeypatch):
    output = Path("tests/_runtime_history_release")
    write_site(output, [public_record()], retention_days=30)
    monkeypatch.setattr(
        publisher,
        "utc_now",
        lambda: datetime(2026, 7, 16, 12, 30, tzinfo=timezone.utc),
    )
    fake = FakeWriteS3()
    pointer = publish_release(fake, "bucket", output, "history/releases", "history/current.json", 30)
    assert [item["Key"] for item in fake.writes][-1] == "history/current.json"
    archive = fake.writes[0]["Body"]
    assert pointer["sha256"] == hashlib.sha256(archive).hexdigest()
    assert pointer["object_key"].startswith("history/releases/20260716T123000Z-")
    assert json.loads(fake.writes[-1]["Body"]) == pointer


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
            projections[key] = {"records": [public_record(id=f"{date}-{slot}", date=date)]}
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
