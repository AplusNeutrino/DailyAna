from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def rules() -> dict:
    path = Path(__file__).parents[1] / "config" / "intelligence_rules.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def intelligence_config(rules: dict) -> dict:
    return {
        "slots": {"A": {"time": "06:00"}, "B": {"time": "14:15"}, "C": {"time": "18:10"}},
        "scoring": {"thresholds": {"altar": 80, "brief": 60, "list": 0}},
        "rules": rules,
        "storage": {"archive": {"enabled": True}},
        "migration": {"dual_write_legacy_until": "2099-01-01"},
    }


def report(*rows: dict) -> dict:
    return {"stats": [{"word": "test", "titles": list(rows)}]}
