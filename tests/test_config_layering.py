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


def test_scheduled_profile_sources_are_disjoint():
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

    expected_platforms = {
        "work": {"wallstreetcn-hot", "cls-hot", "zhihu"},
        "general": {"baidu", "thepaper", "toutiao", "ifeng"},
        "relax": {"bilibili-hot-search", "tieba", "weibo", "douyin"},
    }
    platform_sets = {
        profile: sources_for(profile, "platforms") for profile in expected_platforms
    }
    rss_sets = {
        profile: sources_for(profile, "rss_feeds") for profile in expected_platforms
    }

    assert platform_sets == expected_platforms
    assert all(not rss_ids for rss_ids in rss_sets.values())
    for profile, sources in platform_sets.items():
        others = set().union(
            *(items for name, items in platform_sets.items() if name != profile)
        )
        assert sources.isdisjoint(others)


def test_private_abc_profiles_filter_platforms_and_rss(monkeypatch):
    root = Path("tests/_runtime_config/abc")
    public = root / "public" / "config.yaml"
    private_dir = root / "private"
    private = private_dir / "config.yaml"
    platforms = [
        {"id": "platform-a", "name": "A"},
        {"id": "platform-b", "name": "B"},
        {"id": "platform-c", "name": "C"},
    ]
    feeds = [
        {"id": "feed-a", "name": "A", "url": "https://rsshub.app/example/a"},
        {"id": "feed-b", "name": "B", "url": "https://example.com/b.xml"},
        {"id": "feed-c", "name": "C", "url": "https://rsshub.app/example/c"},
    ]
    write_yaml(
        public,
        {
            "platforms": {"enabled": True, "sources": platforms},
            "rss": {"enabled": True, "feeds": feeds},
        },
    )
    write_yaml(private, {"rss": {"rsshub_base_url": "https://rsshub.example"}})
    write_yaml(
        private_dir / "content_categories.yaml",
        {
            "categories": {
                "work_academic": {
                    "platforms": ["platform-a"],
                    "rss_feeds": ["feed-a"],
                },
                "general_news": {
                    "platforms": ["platform-b"],
                    "rss_feeds": ["feed-b"],
                },
                "entertainment": {
                    "platforms": ["platform-c"],
                    "rss_feeds": ["feed-c"],
                },
            }
        },
    )
    profile_categories = {
        "work": "work_academic",
        "general": "general_news",
        "relax": "entertainment",
    }
    for profile, category in profile_categories.items():
        write_yaml(
            private_dir / "profiles" / f"{profile}.yaml",
            {"content": {"selected_categories": [category]}},
        )

    monkeypatch.setenv("RAVENIS_PRIVATE_CONFIG", str(private.resolve()))
    monkeypatch.setenv("RAVENIS_PRIVATE_CONFIG_DIR", str(private_dir.resolve()))
    expected_urls = {
        "work": "https://rsshub.example/example/a",
        "general": "https://example.com/b.xml",
        "relax": "https://rsshub.example/example/c",
    }
    for profile, suffix in (("work", "a"), ("general", "b"), ("relax", "c")):
        monkeypatch.setenv("DAILYANA_PROFILE", profile)
        loaded = load_config(str(public))
        assert [item["id"] for item in loaded["PLATFORMS"]] == [f"platform-{suffix}"]
        assert [item["id"] for item in loaded["RSS"]["FEEDS"]] == [f"feed-{suffix}"]
        assert loaded["RSS"]["FEEDS"][0]["url"] == expected_urls[profile]
