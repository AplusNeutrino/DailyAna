from __future__ import annotations

from copy import deepcopy
from datetime import datetime

import pytest
from conftest import report

from trendradar.ai import AIAnalyzer
from trendradar.intelligence import (
    TitleIntegrityError,
    build_digest_summary,
    build_intelligence_package,
    build_public_projection,
    normalize_display_title,
    render_wechat_intelligence_messages,
    select_digest_candidates,
)
from trendradar.notification.wework import _plain_text


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


def test_publisher_aliases_keep_source_counts_consistent(intelligence_config, monkeypatch):
    title = "半导体材料价格上涨，晶圆厂更新采购计划"
    package = build(
        intelligence_config,
        monkeypatch,
        {"title": title, "url": "https://one.example/a", "source": "华尔街见闻", "source_id": "wallstreetcn-hot"},
        {"title": title, "url": "https://two.example/b", "source": "华尔街见闻", "source_id": "wallstreetcn"},
        {"title": title, "url": "https://three.example/c", "source": "财联社", "source_id": "cls-hot"},
    )
    item = package["raw_items"][0]
    assert item["source_count"] == 2
    assert len(item["sources"]) == 2
    config = editorial_config(intelligence_config)
    message = render_wechat_intelligence_messages(package, config=config)[0]
    assert "来源：华尔街见闻、财联社 · 2 个独立发布者" in message
    assert "单一发布者" not in message


def test_display_title_normalization_is_lossless():
    title = "  [早报][早报]  路透：ASML Q2营收增长77.4%！  "
    assert normalize_display_title(title) == "[早报] 路透：ASML Q2营收增长77.4%！"


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
    assert record["source_count"] == 1
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
        "max_watch_items": 2,
        "detail_link_enabled": False,
        "detail_url": "https://example.com/history/",
    }
    return config


def screenshot_rows():
    titles = (
        "半导体抛售潮拖累美股，纳指100一度跌2%，中概股逆市上涨，黄金大跌破4000",
        "台积电Q2净利润同比增长77.4%大超预期，毛利率达67.7%，AI芯片需求强劲势头持续",
        "[早报] 美股存储股再遭重挫；美军连续空袭伊朗；油价、金价双双下跌，多只芯片股发布重要公告",
        "涉晶圆刻蚀、清洗等环节，两大半导体材料齐涨价",
        "美股收盘：芯片股持续下挫，存储、光通信板块领跌",
        "业绩预增，再签新单，AI芯片三龙头携手“报喜”",
        "财联社记者探营WAIC 2026：从超节点算力到进厂机器人，AI与物理世界开启新一轮融合",
        "DeepSeek实习薪资突破5500元，这反映了AI行业与求职市场哪些现状？",
        "OpenAI发布新一代GPT模型评测结果，并公布开发者API迁移时间表",
        "国产大模型价格再下调，企业客户开始重新评估推理成本",
        "英伟达公布新款GPU供货节奏，服务器厂商同步更新交付预期",
        "HBM现货价格连续第三周上涨，存储产业链关注新增产能释放",
        "光模块企业披露海外订单进展，AI数据中心需求仍是核心变量",
        "液冷服务器方案进入集中验证期，多家厂商公布测试数据",
        "半导体设备企业获得批量订单，国产替代继续向关键环节推进",
        "美联储官员谈通胀与降息路径，市场重新定价年内利率预期",
        "人民币汇率波动收窄，出口企业关注结汇节奏变化",
        "黄金价格快速回落后企稳，避险资金是否撤出仍待数据确认",
        "国际油价下跌，能源股和新能源板块出现分化走势",
        "欧洲科技股普遍调整，芯片与软件公司领跌主要指数",
        "港股科技板块午后回升，南向资金净流入规模扩大",
        "A股成交额回升，算力和机器人方向获得增量资金关注",
        "美国监管机构发布AI风险管理新指引，企业合规成本或上升",
        "国内发布数据要素流通政策征求意见稿，强调安全与授权边界",
        "自动驾驶企业公布新一轮城市开放计划，量产和事故责任仍是关键",
        "机器人厂商获得汽车工厂订单，交付数量和稳定性等待验证",
        "特斯拉更新FSD测试范围，监管部门要求补充安全数据",
        "小鹏汽车披露智能驾驶订阅数据，用户付费率出现改善",
        "GitHub推出代码审查新功能，开源维护者可配置自动检查规则",
        "Python社区发布性能优化提案，计划在后续版本逐步落地",
        "开源数据库完成重要版本升级，并给出生产迁移兼容性说明",
        "开发者工具企业公布商业化数据，团队席位收入同比增长",
        "新能源车企下调部分车型售价，供应链成本与竞争压力并存",
        "储能项目招标规模增加，电芯价格与并网进度成为观察重点",
        "韩国叫停新单一股票杠杆ETF产品上市，并拟加强风险披露",
        "日本央行维持政策不变，日元和亚洲科技资产短线波动",
        "英国公布数字市场监管进展，多家平台需要调整数据政策",
        "游戏公司公布暑期新品排期，首周流水和留存率仍待验证",
        "国际足球赛事版权达成新协议，流媒体平台争夺继续升温",
    )
    assert len(titles) == 39
    sources = ("华尔街见闻", "财联社", "新华社", "行业公告")
    source_ids = ("wallstreetcn-hot", "cls-hot", "xinhua", "industry-notice")
    return [
        {
            "title": title,
            "url": f"https://example.com/{index}",
            "source": sources[(index - 1) % len(sources)],
            "source_id": source_ids[(index - 1) % len(source_ids)],
            "rank": index,
        }
        for index, title in enumerate(titles, 1)
    ]


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


def displayed_news_titles(message):
    titles = []
    section = ""
    for line in message.splitlines():
        if line.endswith("更多标题"):
            section = "more"
            continue
        if line.endswith("继续观察"):
            section = "watch"
            continue
        if line.startswith("**") and line.endswith("**"):
            body = line[2:-2]
            if body[:1].isdigit() and ". " in body:
                titles.append(body.split(". ", 1)[1])
        elif section == "more" and line.startswith("• "):
            titles.append(line[2:])
    return titles


def test_editorial_digest_is_one_mobile_message(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    digest = build_digest_summary(package, valid_ai_digest(package), config)
    messages = render_wechat_intelligence_messages(package, digest, config)
    assert len(messages) == 1
    assert len(messages[0].encode("utf-8")) <= 3200
    assert "Ravenis 午间简报 · 7月16日" in messages[0]
    assert "先看这 3 件事" in messages[0]
    assert "39 条入库 · 3 项重点" in messages[0]
    assert "更多标题" in messages[0]
    assert "查看完整日报" not in messages[0]
    assert "类更新" not in messages[0]
    assert "条新信号" not in messages[0]
    assert "发生：" not in messages[0]
    assert "量子祈坛" not in messages[0]
    assert "知识之塔" not in messages[0]
    assert "B001" not in messages[0]
    original_titles = {
        normalize_display_title(item["title"])
        for item in package["raw_items"]
    }
    shown_titles = displayed_news_titles(messages[0])
    assert 3 <= len(shown_titles) <= 9
    assert len(shown_titles) == len(set(shown_titles))
    assert set(shown_titles) <= original_titles
    assert all("…" not in title and "..." not in title for title in shown_titles)


@pytest.mark.parametrize(
    ("slot", "label"),
    (("A", "早间"), ("B", "午间"), ("C", "晚间")),
)
def test_editorial_detail_link_is_explicit_and_slot_safe(
    intelligence_config, monkeypatch, slot, label
):
    package, config = build_editorial_package(intelligence_config, monkeypatch, slot=slot)
    config["wechat"]["detail_link_enabled"] = True
    digest = build_digest_summary(package, valid_ai_digest(package), config)
    message = render_wechat_intelligence_messages(package, digest, config)[0]
    assert f"Ravenis {label}简报" in message
    assert "查看完整日报（39 条）" in message
    assert "date=2026-07-16" in message
    assert f"slot={slot}" in message


def test_plain_text_fallback_converts_markdown_links(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    config["wechat"]["detail_link_enabled"] = True
    digest = build_digest_summary(package, valid_ai_digest(package), config)
    message = render_wechat_intelligence_messages(
        package,
        digest,
        config,
        output_format="text",
    )[0]
    assert "[查看完整日报" not in message
    assert "查看完整日报（39 条）：https://example.com/history/?" in message
    assert "# Ravenis" not in message
    assert _plain_text("[查看完整日报](https://example.com/ravenis/) →") == (
        "查看完整日报：https://example.com/ravenis/ →"
    )


def test_invalid_or_generic_ai_digest_falls_back(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    bad = valid_ai_digest(package)
    bad["top_items"][0]["id"] = "r_unknown"
    bad["top_items"][0]["evidence_ids"] = ["r_unknown"]
    assert build_digest_summary(package, bad, config).status == "rules"

    generic = valid_ai_digest(package)
    generic["top_items"][0]["impact"] = "值得关注"
    assert build_digest_summary(package, generic, config).status == "rules"

    contradictory = valid_ai_digest(package)
    contradictory["top_items"][0]["impact"] = "由 2 个独立来源交叉验证，可信度较高。"
    assert build_digest_summary(package, contradictory, config).status == "rules"


def test_over_target_prunes_secondary_items_not_titles(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    config["wechat"]["target_bytes"] = 1200
    digest = build_digest_summary(package, valid_ai_digest(package), config)
    item_by_id = {item["id"]: item for item in package["raw_items"]}
    complete_titles = []
    for index, top in enumerate(digest.top_items, 1):
        title = f"{item_by_id[top.id]['title']}，第{index}项完整补充" + "完整信息" * 40
        item_by_id[top.id]["title"] = title
        complete_titles.append(title)

    messages = render_wechat_intelligence_messages(package, digest, config)
    assert len(messages) == 1
    assert len(messages[0].encode("utf-8")) <= 4000
    shown_titles = displayed_news_titles(messages[0])
    assert all(title in shown_titles for title in complete_titles)
    assert len(shown_titles) - len(complete_titles) <= 3
    assert all("…" not in title for title in shown_titles)


def test_over_hard_limit_splits_only_between_complete_items(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    digest = build_digest_summary(package, valid_ai_digest(package), config)
    item_by_id = {item["id"]: item for item in package["raw_items"]}
    complete_titles = []
    for index, top in enumerate(digest.top_items, 1):
        title = f"超长标题{index}：" + ("完整语义不得截断" * 75)
        item_by_id[top.id]["title"] = title
        complete_titles.append(title)

    messages = render_wechat_intelligence_messages(package, digest, config, max_bytes=4000)
    assert len(messages) >= 2
    assert all(len(message.encode("utf-8")) <= 4000 for message in messages)
    for title in complete_titles:
        assert sum(title in message for message in messages) == 1
        assert "…" not in title and "..." not in title


def test_single_impossible_title_fails_instead_of_truncating(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    digest = build_digest_summary(package, valid_ai_digest(package), config)
    item_by_id = {item["id"]: item for item in package["raw_items"]}
    item_by_id[digest.top_items[0].id]["title"] = "完整标题" * 1500
    with pytest.raises(TitleIntegrityError):
        render_wechat_intelligence_messages(package, digest, config, max_bytes=4000)


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


def test_editorial_ai_accepts_fenced_json_with_leading_text(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    calls = []

    class FencedClient:
        api_key = "test-key"

        def chat(self, messages, **kwargs):
            calls.append((messages, kwargs))
            import json

            payload = json.dumps({"digest_summary": valid_ai_digest(package)}, ensure_ascii=False)
            return f"以下是摘要：\n```json\n{payload}\n```\n"

    analyzer = AIAnalyzer(
        {"MODEL": "test/dsv4pro", "API_KEY": "test-key"},
        {"PROMPT_FILE": "intelligence_digest_prompt.txt"},
        datetime.now,
    )
    analyzer.client = FencedClient()
    result = analyzer.analyze_intelligence_package(package, config["summary"])

    assert result.success
    assert result.digest_summary["top_items"]
    assert len(calls) == 1


def test_editorial_ai_repairs_json_locally_without_second_model_call(
    intelligence_config, monkeypatch
):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    calls = []

    class RepairableClient:
        api_key = "test-key"

        def chat(self, messages, **kwargs):
            calls.append((messages, kwargs))
            import json

            payload = json.dumps({"digest_summary": valid_ai_digest(package)}, ensure_ascii=False)
            return payload[:-1] + ",}"

    analyzer = AIAnalyzer(
        {"MODEL": "test/dsv4pro", "API_KEY": "test-key"},
        {"PROMPT_FILE": "intelligence_digest_prompt.txt"},
        datetime.now,
    )
    analyzer.client = RepairableClient()
    result = analyzer.analyze_intelligence_package(package, config["summary"])

    assert result.success
    assert len(calls) == 1
    assert build_digest_summary(package, result.digest_summary, config).status == "ai"


def test_editorial_ai_rejects_response_without_json(intelligence_config, monkeypatch):
    package, config = build_editorial_package(intelligence_config, monkeypatch)
    calls = []

    class BrokenClient:
        api_key = "test-key"

        def chat(self, messages, **kwargs):
            calls.append((messages, kwargs))
            return "抱歉，本次无法生成结构化摘要。"

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
