from __future__ import annotations

from types import SimpleNamespace

import pytest

from trendradar.__main__ import NewsAnalyzer


def test_prepare_standalone_includes_all_active_sources() -> None:
    analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
    analyzer.ctx = SimpleNamespace(
        config={
            "DISPLAY": {
                "STANDALONE": {
                    "PLATFORMS": [],
                    "RSS_FEEDS": [],
                    "MAX_ITEMS": 20,
                    "INCLUDE_ALL_ACTIVE_SOURCES": True,
                }
            }
        },
        platform_ids=["weibo"],
        rss_feeds=[{"id": "folo-entertainment"}],
    )

    standalone = analyzer._prepare_standalone_data(
        results={
            "weibo": {
                "完整热榜标题": {
                    "url": "https://example.com/hot",
                    "ranks": [1],
                }
            }
        },
        id_to_name={"weibo": "微博"},
        rss_items=[
            {
                "feed_id": "folo-entertainment",
                "feed_name": "娱乐订阅",
                "title": "完整 RSS 标题",
                "url": "https://example.com/rss",
                "summary": "用于摘要的原始说明",
            }
        ],
    )

    assert standalone is not None
    assert standalone["platforms"][0]["id"] == "weibo"
    assert standalone["rss_feeds"][0]["id"] == "folo-entertainment"
    assert standalone["rss_feeds"][0]["items"][0]["summary"] == "用于摘要的原始说明"


def test_notification_sends_when_only_standalone_content_exists() -> None:
    dispatched = []

    class Dispatcher:
        def dispatch_all(self, **kwargs):
            dispatched.append(kwargs)
            return {"wework": True}

    analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
    analyzer.ctx = SimpleNamespace(
        config={
            "ENABLE_NOTIFICATION": True,
            "AI_ANALYSIS": {"ENABLED": False},
            "SHOW_VERSION_UPDATE": False,
        },
        platform_ids=["weibo"],
        prepare_report=lambda *args, **kwargs: {"stats": []},
        create_notification_dispatcher=lambda: Dispatcher(),
    )
    analyzer._has_notification_configured = lambda: True
    analyzer._has_valid_content = lambda stats, new_titles: False
    analyzer._get_mode_strategy = lambda: {"mode_name": "当前榜单模式"}
    analyzer._hotlist_total_count = 0
    analyzer._rss_matched_count = 0
    analyzer._rss_total_count = 1
    analyzer._rss_source_total = 1
    analyzer._rss_source_failed = 0
    analyzer.frequency_file = None
    analyzer.update_info = None
    analyzer.proxy_url = None

    result = analyzer._send_notification_if_needed(
        stats=[],
        report_type="当前榜单",
        mode="current",
        standalone_data={
            "platforms": [],
            "rss_feeds": [
                {
                    "id": "folo-entertainment",
                    "name": "娱乐订阅",
                    "items": [{"title": "完整 RSS 标题"}],
                }
            ],
        },
        schedule=SimpleNamespace(
            push=True,
            once_push=False,
            period_key=None,
            period_name=None,
        ),
    )

    assert result is True
    assert len(dispatched) == 1
    assert dispatched[0]["standalone_data"]["rss_feeds"][0]["items"]


def test_github_actions_reraises_pipeline_failure() -> None:
    analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
    analyzer.ctx = SimpleNamespace(config={}, cleanup=lambda: None)
    analyzer.is_github_actions = True
    analyzer._initialize_and_check_config = lambda: (_ for _ in ()).throw(
        RuntimeError("notification failed")
    )

    with pytest.raises(RuntimeError, match="notification failed"):
        analyzer.run()
