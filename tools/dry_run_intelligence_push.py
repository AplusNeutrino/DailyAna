#!/usr/bin/env python3
# coding=utf-8
"""Dry-run Ravenis Core intelligence push rendering without sending messages."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trendradar.intelligence import build_intelligence_package, render_wechat_intelligence_messages


def sample_titles():
    seeds = [
        ("DeepSeek 新模型引发开发者讨论", "Hacker News", "rss"),
        ("英伟达市值再创新高，AI 算力需求持续增长", "Yahoo Finance", "rss"),
        ("Qwen 工具链更新，开发者生态继续扩张", "GitHub Trending", "rss"),
        ("存储芯片价格上涨引发市场关注", "财联社", "hotlist"),
        ("机器人公司获得新一轮融资", "36氪", "rss"),
        ("FSD 更新后自动驾驶责任讨论升温", "知乎", "hotlist"),
        ("美联储利率路径影响科技股情绪", "华尔街见闻", "hotlist"),
        ("热门游戏新版本上线", "bilibili 热搜", "hotlist"),
        ("食品安全事件引发监管关注", "百度热搜", "hotlist"),
    ]
    rows = []
    for idx in range(45):
        title, source, source_type = seeds[idx % len(seeds)]
        rows.append({
            "title": f"{title} #{idx + 1}",
            "source_name": source,
            "source_id": source.lower().replace(" ", "-"),
            "url": f"https://example.com/news/{idx + 1}",
            "rank": (idx % 20) + 1,
            "crawl_time": "06:00",
            "summary": "",
            "source_type": source_type,
        })
    return rows


def main() -> None:
    titles = sample_titles()
    report_data = {
        "stats": [
            {"word": "AI", "count": 20, "titles": titles[:20]},
            {"word": "市场", "count": 15, "titles": titles[20:35]},
            {"word": "休闲", "count": 10, "titles": titles[35:]},
        ],
        "new_titles": [],
        "failed_ids": [],
        "total_new_count": 0,
    }
    config = {
        "enabled": True,
        "slots": {"A": {"time": "06:00"}, "B": {"time": "12:00"}, "C": {"time": "18:00"}},
        "scoring": {"thresholds": {"altar": 80, "brief": 60, "list": 30}},
        "wechat": {
            "max_items_per_category": 12,
            "max_clusters_in_altar": 5,
            "max_clusters_in_trial": 5,
            "max_observations": 6,
        },
    }
    package = build_intelligence_package(
        report_data=report_data,
        now=datetime(2026, 6, 19, 6, 0),
        config=config,
    )
    messages = render_wechat_intelligence_messages(package, config, max_bytes=4000)
    assert len(package["raw_items"]) == 45
    assert package["raw_items"][0]["id"] == "20260619A001"
    assert package["raw_items"][-1]["id"] == "20260619A045"
    assert all("http://" not in msg and "https://" not in msg for msg in messages)
    assert all(len(msg.encode("utf-8")) <= 4000 for msg in messages)
    print(f"raw_items={len(package['raw_items'])}")
    print(f"clusters={len(package['clusters'])}")
    print(f"messages={len(messages)}")
    for idx, message in enumerate(messages):
        print(f"\n--- message {idx} ({len(message.encode('utf-8'))} bytes) ---")
        print(message[:1200])


if __name__ == "__main__":
    main()
