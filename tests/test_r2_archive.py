from __future__ import annotations

from datetime import datetime

from conftest import report

import trendradar.intelligence as intelligence


class FakeS3:
    def __init__(self):
        self.keys = []

    def put_object(self, **kwargs):
        self.keys.append(kwargs["Key"])


def test_reruns_use_distinct_run_keys_and_content_keys(intelligence_config, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(intelligence, "_s3_client_from_config", lambda config: (fake, "bucket"))
    monkeypatch.setenv("RAVENIS_SLOT", "A")
    packages = []
    for title, run_id in (("OpenAI GPT 发布模型甲", "101"), ("OpenAI GPT 发布模型乙", "102")):
        monkeypatch.setenv("GITHUB_RUN_ID", run_id)
        package = intelligence.build_intelligence_package(
            report_data=report({"title": title, "url": f"https://example.com/{run_id}", "source_id": "one"}),
            now=datetime(2026, 7, 15, 6, 0),
            config=intelligence_config,
        )
        packages.append(package)
        assert intelligence.archive_intelligence_to_r2(package, ["message"], intelligence_config)
    run_keys = [key for key in fake.keys if key.startswith("runs/")]
    news_keys = [key for key in fake.keys if key.startswith("news/")]
    assert len(set(run_keys)) == 2
    assert len(set(news_keys)) == 2
    assert packages[0]["raw_items"][0]["id"] != packages[1]["raw_items"][0]["id"]
