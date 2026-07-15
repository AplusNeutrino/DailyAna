from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from conftest import report

from trendradar.intelligence import build_intelligence_package, build_public_projection


def build(config, monkeypatch, *rows):
    monkeypatch.setenv("RAVENIS_SLOT", "A")
    return build_intelligence_package(
        report_data=report(*rows),
        now=datetime(2026, 7, 15, 6, 0),
        config=config,
    )


def test_stable_id_survives_rerun(intelligence_config, monkeypatch):
    row = {"title": "OpenAI GPT 发布新模型", "url": "https://example.com/a?utm_source=x", "source_id": "one"}
    first = build(intelligence_config, monkeypatch, row)
    second = build(intelligence_config, monkeypatch, row)
    assert first["raw_items"][0]["id"] == second["raw_items"][0]["id"]
    assert first["raw_items"][0]["short_id"] == "A001"


def test_dedupe_merges_independent_sources(intelligence_config, monkeypatch):
    package = build(
        intelligence_config,
        monkeypatch,
        {"title": "OpenAI GPT 发布新模型", "url": "https://one.example/a", "source": "One", "source_id": "one"},
        {"title": "OpenAI GPT 发布新模型", "url": "https://two.example/b", "source": "Two", "source_id": "two"},
    )
    item = package["raw_items"][0]
    assert len(package["raw_items"]) == 1
    assert item["source_count"] == 2
    assert item["occurrence_count"] == 2
    assert {source["source_id"] for source in item["sources"]} == {"one", "two"}


def test_scoring_weights_are_effective(intelligence_config, monkeypatch):
    relevance = deepcopy(intelligence_config)
    relevance["rules"]["scoring"]["weights"] = {
        "relevance": 1.0, "source_quality": 0.0, "multi_source": 0.0, "novelty": 0.0, "impact": 0.0,
    }
    quality = deepcopy(intelligence_config)
    quality["rules"]["scoring"]["weights"] = {
        "relevance": 0.0, "source_quality": 1.0, "multi_source": 0.0, "novelty": 0.0, "impact": 0.0,
    }
    row = {"title": "OpenAI GPT 发布新模型", "source_id": "weibo", "source_type": "hotlist"}
    assert build(relevance, monkeypatch, row)["raw_items"][0]["raw_item_score"] > build(quality, monkeypatch, row)["raw_items"][0]["raw_item_score"]


def test_cluster_requires_similarity_and_distinct_sources(intelligence_config, monkeypatch):
    package = build(
        intelligence_config,
        monkeypatch,
        {"title": "OpenAI GPT 发布新模型能力", "source_id": "one"},
        {"title": "OpenAI GPT 发布新模型能力升级", "source_id": "two"},
        {"title": "OpenAI 公司股票今日上涨", "source_id": "three"},
    )
    assert len(package["clusters"]) == 1
    assert package["clusters"][0]["source_count"] == 2
    assert len(package["clusters"][0]["related_item_ids"]) == 2


def test_public_projection_is_strict_and_safe(intelligence_config, monkeypatch):
    package = build(
        intelligence_config,
        monkeypatch,
        {"title": "OpenAI GPT 发布新模型", "url": "javascript:alert(1)", "source_id": "one", "summary": "x" * 400},
    )
    projection = build_public_projection(package)
    record = projection["records"][0]
    assert record["url"] == ""
    assert len(record["summary"]) == 280
    assert "raw_payload" not in record
    assert "score_components" not in record
