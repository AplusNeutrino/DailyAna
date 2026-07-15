from __future__ import annotations

import pytest

from trendradar.ai_digest_contract import validate_digest_analysis
from trendradar.notification import wework
from trendradar.notification.wework import send_wework_messages, split_utf8_text
from trendradar.storage.manager import StorageManager


def test_ai_digest_requires_exact_ids():
    validate_digest_analysis(
        {"items": [{"digest_id": "a"}, {"digest_id": "b"}], "daily": {}},
        ["a", "b"],
    )
    with pytest.raises(ValueError):
        validate_digest_analysis({"items": [{"digest_id": "a"}], "daily": {}}, ["a", "b"])
    with pytest.raises(ValueError):
        validate_digest_analysis({"items": [{"digest_id": "a"}, {"digest_id": "a"}], "daily": {}}, ["a"])


def test_wework_split_is_utf8_byte_safe():
    chunks = split_utf8_text("汉字" * 3000, max_bytes=4000)
    assert len(chunks) > 1
    assert all(len(chunk.encode("utf-8")) <= 4000 for chunk in chunks)


def test_wework_failure_is_not_swallowed(monkeypatch):
    class Response:
        status_code = 500

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(wework.requests, "post", lambda *args, **kwargs: Response())
    assert not send_wework_messages(["https://example.invalid"], ["message"], interval=0)


def test_primary_storage_batch_failure_raises():
    class Backend:
        @staticmethod
        def end_batch():
            return False

    manager = StorageManager()
    manager._backend = Backend()
    with pytest.raises(RuntimeError, match="primary storage"):
        manager.end_batch()
