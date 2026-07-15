from __future__ import annotations

from pathlib import Path

import yaml

from trendradar.core.loader import load_config


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
