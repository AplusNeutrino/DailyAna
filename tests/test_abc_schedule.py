from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_production_schedule_has_three_explicit_briefs_and_digest():
    crawler = (ROOT / ".github" / "workflows" / "crawler.yml").read_text(
        encoding="utf-8"
    )
    digest = (ROOT / ".github" / "workflows" / "ai-digest-daily.yml").read_text(
        encoding="utf-8"
    )

    assert set(re.findall(r'cron:\s*"([^"]+)"', crawler)) == {
        "0 22 * * *",
        "5 6 * * *",
        "10 9 * * *",
    }
    assert '"0 22 * * *") slot=A; profile=work' in crawler
    assert '"5 6 * * *") slot=B; profile=general' in crawler
    assert '"10 9 * * *") slot=C; profile=relax' in crawler
    assert "options: [work, general, relax]" in crawler
    assert set(re.findall(r'cron:\s*"([^"]+)"', digest)) == {"0 2 * * *"}


def test_slot_labels_match_beijing_schedule():
    config = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert config["slots"] == {
        "A": {"label": "早间", "time": "06:00"},
        "B": {"label": "午间", "time": "14:05"},
        "C": {"label": "晚间", "time": "17:10"},
    }
