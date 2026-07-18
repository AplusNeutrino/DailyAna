from __future__ import annotations

from pathlib import Path

import yaml

from trendradar.core.loader import load_config

ROOT = Path(__file__).parents[1]


def write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_config_precedence_public_private_profile_env(monkeypatch):
    root = Path("tests/_runtime_config")
    public = root / "public" / "config.yaml"
    private_dir = root / "private"
    private = private_dir / "config.yaml"
    write_yaml(
        public,
        {
            "app": {"timezone": "UTC"},
            "ai": {"model": "public-model"},
            "notification": {"enabled": False},
            "platforms": {"sources": []},
            "rss": {"feeds": []},
        },
    )
    write_yaml(private, {"ai": {"model": "private-model"}})
    write_yaml(private_dir / "profiles" / "work.yaml", {"notification": {"enabled": True}})
    monkeypatch.setenv("RAVENIS_PRIVATE_CONFIG", str(private.resolve()))
    monkeypatch.setenv("RAVENIS_PRIVATE_CONFIG_DIR", str(private_dir.resolve()))
    monkeypatch.setenv("DAILYANA_PROFILE", "work")
    monkeypatch.setenv("AI_MODEL", "environment-model")
    loaded = load_config(str(public))
    assert loaded["AI"]["MODEL"] == "environment-model"
    assert loaded["ENABLE_NOTIFICATION"] is True
    assert loaded["CONFIG_SOURCES"]["private"] == str(private.resolve())


def test_work_and_relax_sources_are_disjoint():
    categories = yaml.safe_load(
        (ROOT / "config" / "content_categories.yaml").read_text(encoding="utf-8")
    )["categories"]

    def sources_for(profile_name: str, field: str) -> set[str]:
        profile = yaml.safe_load(
            (ROOT / "config" / "profiles" / f"{profile_name}.yaml").read_text(
                encoding="utf-8"
            )
        )
        selected = profile["content"]["selected_categories"]
        return {
            source_id
            for category_id in selected
            for source_id in categories[category_id].get(field, [])
        }

    work_platforms = sources_for("work", "platforms")
    relax_platforms = sources_for("relax", "platforms")
    work_rss = sources_for("work", "rss_feeds")
    relax_rss = sources_for("relax", "rss_feeds")

    assert work_platforms == {
        "baidu", "thepaper", "wallstreetcn-hot", "cls-hot", "zhihu"
    }
    assert relax_platforms == {
        "toutiao", "ifeng", "bilibili-hot-search", "tieba", "weibo", "douyin"
    }
    assert work_platforms.isdisjoint(relax_platforms)
    assert work_rss == {"hacker-news", "yahoo-finance", "ruanyifeng"}
    assert relax_rss == set()
