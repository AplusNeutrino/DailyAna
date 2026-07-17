from __future__ import annotations

from trendradar.weekly import build_weekly_digest, render_weekly_markdown


def record(day: int, theme: str, index: int, score: int = 75) -> dict:
    return {
        "id": f"r_{theme}_{day}_{index}",
        "date": f"2026-07-{day:02d}",
        "title": f"{theme} 新闻 {index}",
        "source": f"来源{index % 3}",
        "category": "AI / 模型",
        "tags": [theme],
        "score": score,
        "last_seen": f"2026-07-{day:02d}T12:00:00",
    }


def test_weekly_digest_distinguishes_new_reinforced_and_cooled():
    records = []
    records.extend(record(day, "旧主题", index) for day in (10, 11, 12) for index in range(2))
    records.extend(record(day, "增强主题", index, 82) for day in (10, 14, 15, 16) for index in range(2))
    records.extend(record(day, "新增主题", index, 88) for day in (14, 15, 16) for index in range(2))
    digest = build_weekly_digest(records, end_date="2026-07-16")
    statuses = {theme["name"]: theme["status"] for theme in digest["model_candidates"]}
    assert statuses["旧主题"] == "cooled"
    assert statuses["增强主题"] == "reinforced"
    assert statuses["新增主题"] == "new"
    assert len(digest["model_candidates"]) <= 12
    assert len(digest["top_themes"]) == 3


def test_weekly_markdown_is_synthesis_not_daily_concatenation():
    records = [record(day, "模型价格", day, 80 + (day % 3)) for day in range(10, 17)]
    digest = build_weekly_digest(records, end_date="2026-07-16")
    message = render_weekly_markdown(digest, "https://example.com/history/?view=weekly")
    assert "本周主线" in message
    assert "影响：" in message
    assert "观察：" in message
    assert "查看本周完整证据" in message
    assert len(message.encode("utf-8")) < 3200
