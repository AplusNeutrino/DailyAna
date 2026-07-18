from __future__ import annotations

from datetime import datetime

from tools.fetch_ai_digest_daily import DigestTopic, rank_ai_digest_topics
from trendradar.ranking import ScoringContext, perspective_for_slot, score_record


def context(rules, **override):
    return ScoringContext(
        rules=rules,
        now=datetime(2026, 7, 18, 10, 0),
        recent_records=override.get("recent_records", {}),
        history_status=override.get("history_status", "ok"),
    )


def record(**override):
    value = {
        "id": "r_test",
        "title": "OpenAI 发布新模型与开发者 API 迁移时间表",
        "summary": "公布版本、价格和迁移截止日期。",
        "category_id": "ai_models",
        "category": "AI / 模型",
        "tags": ["OpenAI"],
        "source": "官方文档",
        "source_id": "openai.com",
        "source_type": "official",
        "source_count": 1,
        "sources": [{
            "source": "OpenAI",
            "source_id": "openai.com",
            "publisher_key": "openai.com",
            "source_type": "official",
            "url": "https://openai.com/news/model",
        }],
        "url": "https://openai.com/news/model",
        "captured_at": "2026-07-18T08:00:00",
        "last_seen": "2026-07-18T08:00:00",
        "platform_rank": 2,
        "occurrence_count": 1,
        "intent": ["WORK_CORE"],
    }
    value.update(override)
    return value


def test_slot_perspectives_are_explicit(rules):
    assert perspective_for_slot("A", rules) == "A"
    assert perspective_for_slot("DIGEST", rules) == "A"
    assert perspective_for_slot("B", rules) == "B"
    assert perspective_for_slot("C", rules) == "C"


def test_same_record_has_distinct_perspective_scores(rules):
    scores = {name: score_record(record(), name, context(rules)).score for name in "ABC"}
    assert scores["A"] > scores["B"]
    assert scores["A"] > scores["C"]
    assert len(set(scores.values())) == 3


def test_public_affairs_is_prioritized_by_b(rules):
    item = record(
        title="国务院发布全国数据安全监管条例，明确企业执行日期",
        summary="政策将于 2026 年 9 月生效。",
        category_id="domestic_policy",
        category="国内政策 / 消费 / 社会",
        tags=["政策"],
        intent=["POLICY"],
    )
    assert score_record(item, "B", context(rules)).score > score_record(item, "C", context(rules)).score


def test_single_social_rumor_is_capped(rules):
    item = record(
        title="网传某游戏疑似今晚停服，引发热议",
        summary="",
        category_id="culture",
        category="娱乐 / 体育 / 游戏",
        source="微博",
        source_id="weibo",
        source_type="social",
        sources=[{
            "source": "微博", "source_id": "weibo", "publisher_key": "weibo",
            "source_type": "social", "url": "https://weibo.com/example",
        }],
        url="https://weibo.com/example",
    )
    result = score_record(item, "C", context(rules))
    assert result.score <= 65
    assert any(value.startswith("rumor:") for value in result.penalties)
    assert any(value.startswith("single_social:") for value in result.penalties)


def test_detailed_sources_override_inflated_source_count(rules):
    item = record(source_count=4)
    result = score_record(item, "A", context(rules))
    assert result.components["evidence"] == 35


def test_offset_timestamp_uses_local_slot_wall_time(rules):
    item = record(captured_at="2026-07-18T08:00:00+08:00")
    result = score_record(item, "A", context(rules))
    assert result.components["recency"] == 100


def test_history_failure_uses_neutral_novelty_and_momentum(rules):
    result = score_record(record(), "A", context(rules, history_status="degraded"))
    assert result.components["novelty"] == 50
    assert result.components["momentum"] == 40


def test_repeated_record_is_not_marked_new(rules):
    current = record()
    recent = {current["id"]: {**current, "fingerprint": "different", "source_count": 1}}
    changed = score_record(current, "A", context(rules, recent_records=recent))
    assert changed.components["novelty"] == 70
    recent[current["id"]] = current
    repeated = score_record(current, "A", context(rules, recent_records=recent))
    assert repeated.components["novelty"] == 25


def test_embedded_first_seen_prevents_repeat_from_becoming_new(rules):
    repeated = record(
        first_seen="2026-07-15T08:00:00",
        last_seen="2026-07-18T08:00:00",
        occurrence_count=3,
    )
    result = score_record(repeated, "A", context(rules))
    assert result.components["novelty"] == 25


def test_ai_digest_forces_a_and_uses_source_domains(intelligence_config):
    official = DigestTopic(
        digest_id="digest-official",
        title="OpenAI 发布新模型 API 与迁移日期",
        primary_url="https://openai.com/news/model",
        page_url="https://example.com/digest",
        published_at="2026-07-18T08:00:00",
        source_urls=["https://openai.com/news/model"],
        sections={"一句话": ["发布新版本。"], "为什么值得关注": ["开发者需要迁移。"]},
    )
    unknown = DigestTopic(
        digest_id="digest-unknown",
        title="个人博客讨论模型体验",
        primary_url="https://unknown.example/post",
        page_url="https://example.com/digest",
        published_at="2026-07-18T08:00:00",
        source_urls=["https://unknown.example/post"],
        sections={"一句话": ["作者分享主观体验。"]},
    )
    ordered, results, records = rank_ai_digest_topics(
        {"INTELLIGENCE_PUSH": intelligence_config}, [unknown, official], "2026-07-18"
    )
    assert all(result.perspective == "A" for result in results.values())
    assert results["digest-official"].components["source_quality"] > results["digest-unknown"].components["source_quality"]
    assert [topic.digest_id for topic in ordered][0] == "digest-official"
    assert records["digest-unknown"]["sources"][0]["source_type"] == "external"
