from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from conftest import report

from trendradar.ai import AIAnalyzer
from trendradar.intelligence import (
    build_digest_summary,
    build_intelligence_package,
    build_public_projection,
    render_wechat_intelligence_messages,
    select_digest_candidates,
)


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


def editorial_config(intelligence_config):
    config = deepcopy(intelligence_config)
    config["layout"] = "editorial_v2"
    config["slots"] = {
        "A": {"time": "06:00", "label": "早间"},
        "B": {"time": "14:15", "label": "午间"},
        "C": {"time": "18:10", "label": "晚间"},
    }
    config["summary"] = {"max_candidates": 12}
    config["wechat"] = {
        "layout": "editorial_v2",
        "target_bytes": 3200,
        "max_top_items": 3,
        "category_item_budget": 6,
        "max_items_per_category": 2,
        "max_watch_items": 3,
        "detail_url": "https://example.com/history/",
    }
    return config


def screenshot_rows():
    rows = []
    groups = (
        ("AI / 模型", "OpenAI GPT 国产模型生态进展"),
        ("芯片 / 算力", "英伟达 GPU 芯片供应变化"),
        ("宏观 / 财经 / 地缘", "美联储 通胀 市场风险变化"),
        ("其他", "韩国 ETF 市场新闻"),
    )
    counts = (8, 7, 8, 8)
    for (category, title), count in zip(groups, counts):
        for index in range(count):
            rows.append({
                "title": f"{title} {index + 1}",
                "url": f"https://example.com/{len(rows) + 1}",
                "source": f"来源{(index % 4) + 1}",
                "source_id": f"source-{category}-{index}",
                "rank": index + 1,
            })
    return rows


def build_editorial_package(intelligence_config, monkeypatch, slot="B"):
    config = editorial_config(intelligence_config)
    monkeypatch.setenv("RAVENIS_SLOT", slot)
    package = build_intelligence_package(
        report_data=report(*screenshot_rows()),
        now=datetime(2026, 7, 16, 14, 15),
        config=config,
    )
    return package, config


def valid_ai_digest(package):
    candidates = select_digest_candidates(package, 12)
    top = candidates[:3]
    return {
        "overview": "本时段的新信号集中在模型、算力和宏观风险。",
        "top_items": [
            {
                "id": item["id"],
                "headline": item["title"][:24],
                "event": item["title"][:56],
                "impact": f"该条综合评分 {item['raw_item_score']}，可与同类信号交叉核验。",
                "watch": "观察是否出现正式数据、产品发布或第二来源。",
                "evidence_ids": [item["id"]],
            }
            for item in top
        ],
        "watchlist": [
            {"text": "观察算力价格是否出现正式报价。", "evidence_ids": [top[0]["id"]]}
        ],
    }


def test_editorial_digest_is_one_mobile_message(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    digest = build_digest_summary(package, valid_ai_digest(package), config)
    messages = render_wechat_intelligence_messages(package, digest, config)
    assert len(messages) == 1
    assert len(messages[0].encode("utf-8")) <= 3200
    assert "Ravenis 午间简报 · 7月16日" in messages[0]
    assert "先看这 3 件事" in messages[0]
    assert "查看完整日报（31 条）" in messages[0]
    assert "date=2026-07-16" in messages[0]
    assert "slot=B" in messages[0]
    assert "量子祈坛" not in messages[0]
    assert "知识之塔" not in messages[0]
    assert "[热]" not in messages[0]
    assert "B001" not in messages[0]


def test_invalid_or_generic_ai_digest_falls_back(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    bad = valid_ai_digest(package)
    bad["top_items"][0]["id"] = "r_unknown"
    bad["top_items"][0]["evidence_ids"] = ["r_unknown"]
    assert build_digest_summary(package, bad, config).status == "rules"

    generic = valid_ai_digest(package)
    generic["top_items"][0]["impact"] = "值得关注"
    assert build_digest_summary(package, generic, config).status == "rules"


def test_public_projection_includes_sanitized_run_summary(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    digest = build_digest_summary(package, valid_ai_digest(package), config)
    render_wechat_intelligence_messages(package, digest, config)
    projection = build_public_projection(package)
    assert projection["schema_version"] == 2
    assert projection["run"]["slot"] == "B"
    assert projection["run"]["summary"]["status"] == "ai"
    assert len(projection["run"]["summary"]["top_items"]) == 3
    assert set(projection["run"]["record_ids"]) == {record["id"] for record in projection["records"]}
    assert "raw_response" not in str(projection)


def test_editorial_ai_uses_one_bounded_low_randomness_call(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    calls = []

    class FakeClient:
        api_key = "test-key"

        def chat(self, messages, **kwargs):
            calls.append((messages, kwargs))
            import json

            return json.dumps({"digest_summary": valid_ai_digest(package)}, ensure_ascii=False)

    analyzer = AIAnalyzer(
        {"MODEL": "test/dsv4pro", "API_KEY": "test-key"},
        {"PROMPT_FILE": "intelligence_digest_prompt.txt"},
        datetime.now,
    )
    analyzer.client = FakeClient()
    result = analyzer.analyze_intelligence_package(package, config["summary"])

    assert result.success
    assert result.digest_summary
    assert result.analyzed_news == 12
    assert len(calls) == 1
    assert calls[0][1] == {"temperature": 0.2, "max_tokens": 900, "num_retries": 1}
    assert sum(item["id"] in calls[0][0][-1]["content"] for item in package["raw_items"]) == 12


def test_editorial_ai_rejects_damaged_json_without_repair_call(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    calls = []

    class BrokenClient:
        api_key = "test-key"

        def chat(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return '{"digest_summary":{"overview":"broken",}}'

    analyzer = AIAnalyzer(
        {"MODEL": "test/dsv4pro", "API_KEY": "test-key"},
        {"PROMPT_FILE": "intelligence_digest_prompt.txt"},
        datetime.now,
    )
    analyzer.client = BrokenClient()
    result = analyzer.analyze_intelligence_package(package, config["summary"])

    assert not result.success
    assert "JSON" in result.error
    assert len(calls) == 1
    assert build_digest_summary(package, result.digest_summary, config).status == "rules"
