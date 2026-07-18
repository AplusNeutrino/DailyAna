from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone

import tools.rescore_history_v3 as migration


def crawler_run() -> dict:
    return {
        "date": "2026-07-18",
        "slot": "C",
        "generated_at": "2026-07-18T17:10:00+08:00",
        "raw_items": [
            {
                "id": "r_tech",
                "short_id": "C001",
                "title": "芯片厂商公布新版本开发工具",
                "summary": "提供 API 与迁移日期。",
                "category_id": "chips_compute",
                "category": "芯片 / 算力",
                "tags": ["芯片"],
                "source": "示例媒体",
                "source_id": "example",
                "source_type": "professional",
                "sources": [{
                    "source": "示例媒体", "source_id": "example",
                    "publisher_key": "example", "source_type": "professional",
                    "url": "https://example.com/tech",
                }],
                "source_count": 1,
                "url": "https://example.com/tech",
                "captured_at": "2026-07-18T16:30:00+08:00",
                "first_seen": "2026-07-18T16:30:00+08:00",
                "last_seen": "2026-07-18T16:30:00+08:00",
                "occurrence_count": 1,
                "platform_rank": 12,
                "intent": ["WORK_CORE"],
            },
            {
                "id": "r_game",
                "short_id": "C002",
                "title": "独立游戏公布上线日期与首发平台",
                "summary": "新作下月正式发售。",
                "category_id": "culture",
                "category": "娱乐 / 体育 / 游戏",
                "tags": ["游戏"],
                "source": "游戏媒体",
                "source_id": "game-media",
                "source_type": "professional",
                "sources": [{
                    "source": "游戏媒体", "source_id": "game-media",
                    "publisher_key": "game-media", "source_type": "professional",
                    "url": "https://example.com/game",
                }],
                "source_count": 1,
                "url": "https://example.com/game",
                "captured_at": "2026-07-18T16:40:00+08:00",
                "first_seen": "2026-07-18T16:40:00+08:00",
                "last_seen": "2026-07-18T16:40:00+08:00",
                "occurrence_count": 1,
                "platform_rank": 2,
                "intent": ["CULTURE"],
            },
        ],
        "clusters": [],
    }


def test_crawler_rescore_builds_c_perspective_v3(rules):
    projection, scored = migration.rescore_crawler_run(crawler_run(), rules, {})
    assert projection["schema_version"] == 3
    assert projection["run"]["perspective"] == "C"
    assert projection["run"]["record_ids"] == ["r_game", "r_tech"]
    assert [item["id"] for item in scored] == ["r_game", "r_tech"]
    assert all(len(item["reasons"]) == 2 for item in projection["run"]["ranking"])


class Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self) -> bytes:
        return self.value


class Paginator:
    def __init__(self, keys: list[str]):
        self.keys = keys

    def paginate(self, **_kwargs):
        return [{"Contents": [{"Key": key} for key in self.keys]}]


class FakeS3:
    def __init__(self, objects: dict[str, bytes], run_keys: list[str]):
        self.objects = dict(objects)
        self.run_keys = run_keys
        self.puts = []

    def get_paginator(self, _name: str):
        return Paginator(self.run_keys)

    def get_object(self, Bucket: str, Key: str):
        del Bucket
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": Body(self.objects[Key])}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]
        self.puts.append(kwargs["Key"])


def test_apply_backs_up_and_verifies_without_touching_private_run(monkeypatch):
    key = "runs/2026/07/18/C/fixture.json.gz"
    private_body = gzip.compress(json.dumps(crawler_run()).encode("utf-8"))
    target = "public-history/2026-07-18/ravenis-C.json"
    old_public = b'{"schema_version":2,"records":[]}'
    fake = FakeS3({key: private_body, target: old_public}, [key])
    monkeypatch.setattr(migration, "s3_client_from_env", lambda: (fake, "bucket"))
    monkeypatch.setattr(
        migration,
        "utc_now",
        lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )
    report = migration.migrate(30, apply=True)
    assert report["run_count"] == 1
    assert json.loads(fake.objects[target])["schema_version"] == 3
    assert any(key.startswith("history/backups/scoring-v2/2026-07-18/ravenis-C-") for key in fake.puts)
    assert fake.objects[key] == private_body
