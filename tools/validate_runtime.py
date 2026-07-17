#!/usr/bin/env python3
"""Read-only validation for Ravenis Core runtime configuration."""

from __future__ import annotations

import argparse
import os
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CRONS = {
    "0 22 * * *": ("A", "work"),
    "15 6 * * *": ("B", "work"),
    "10 10 * * *": ("C", "relax"),
}
REQUIRED_PRODUCTION_ENV = (
    "WEWORK_WEBHOOK_URL",
    "S3_BUCKET_NAME",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_ENDPOINT_URL",
)
REQUIRED_WORKFLOW_SECRETS = {
    "history-publish.yml": {
        "S3_BUCKET_NAME", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
        "S3_ENDPOINT_URL", "NEUTRIVERSE_DISPATCH_TOKEN",
    },
}


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: UniqueKeyLoader, node: yaml.Node, deep: bool = False) -> dict:
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def duplicate_ids(items: list[Any]) -> list[str]:
    values = [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]
    return sorted({value for value in values if values.count(value) > 1})


def validate(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    public_path = ROOT / "config" / "config.yaml"
    try:
        config = read_yaml(public_path)
    except Exception as exc:
        return [f"public config: {exc}"], warnings

    private_value = os.environ.get("RAVENIS_PRIVATE_CONFIG", "").strip()
    if private_value:
        try:
            config = deep_merge(config, read_yaml(Path(private_value)))
        except Exception as exc:
            errors.append(f"private config: {exc}")

    profile_dirs = []
    private_dir = os.environ.get("RAVENIS_PRIVATE_CONFIG_DIR", "").strip()
    if private_dir:
        profile_dirs.append(Path(private_dir) / "profiles")
    profile_dirs.append(ROOT / "config" / "profiles")
    profile_path = next(
        (directory / f"{args.profile}.yaml" for directory in profile_dirs if (directory / f"{args.profile}.yaml").exists()),
        None,
    )
    if not profile_path:
        errors.append(f"profile does not exist: {args.profile}")
    else:
        try:
            config = deep_merge(config, read_yaml(profile_path))
        except Exception as exc:
            errors.append(f"profile {args.profile}: {exc}")

    slots = config.get("slots") or {}
    if args.slot not in slots:
        errors.append(f"slot {args.slot!r} is not defined")
    for slot_id, spec in slots.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_-]*", str(slot_id)):
            errors.append(f"invalid slot ID: {slot_id}")
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", str((spec or {}).get("time", ""))):
            errors.append(f"invalid slot time: {slot_id}")

    for label, items in (
        ("platform", (config.get("platforms") or {}).get("sources") or []),
        ("RSS", (config.get("rss") or {}).get("feeds") or []),
    ):
        duplicates = duplicate_ids(items)
        if duplicates:
            errors.append(f"duplicate {label} IDs: {', '.join(duplicates)}")

    categories_path = Path(private_dir) / "content_categories.yaml" if private_dir else Path()
    if not private_dir or not categories_path.exists():
        categories_path = ROOT / "config" / "content_categories.yaml"
    try:
        category_map = (read_yaml(categories_path).get("categories") or {})
        selected = (config.get("content") or {}).get("selected_categories") or []
        dangling = sorted(set(selected) - set(category_map))
        if dangling:
            errors.append(f"unknown selected content categories: {', '.join(dangling)}")
    except Exception as exc:
        errors.append(f"content categories: {exc}")

    rules_path = Path(private_dir) / "intelligence_rules.yaml" if private_dir else Path()
    if not private_dir or not rules_path.exists():
        rules_path = ROOT / "config" / "intelligence_rules.yaml"
    try:
        rules = read_yaml(rules_path)
        category_rules = rules.get("categories") or []
        duplicates = duplicate_ids(category_rules)
        if duplicates:
            errors.append(f"duplicate intelligence category IDs: {', '.join(duplicates)}")
        category_ids = {item.get("id") for item in category_rules if isinstance(item, dict)}
        relevance_ids = set(((rules.get("scoring") or {}).get("relevance") or {}))
        dangling = sorted(relevance_ids - category_ids)
        if dangling:
            errors.append(f"scoring references unknown categories: {', '.join(dangling)}")
        weights = ((rules.get("scoring") or {}).get("weights") or {})
        required = {"relevance", "source_quality", "multi_source", "novelty", "impact"}
        if set(weights) != required:
            errors.append("scoring.weights must define exactly: " + ", ".join(sorted(required)))
        total = sum(float(value) for value in weights.values())
        if abs(total - 1.0) > 1e-9:
            errors.append(f"scoring.weights must sum to 1.0 (got {total})")
    except Exception as exc:
        errors.append(f"intelligence rules: {exc}")

    crawler_text = (ROOT / ".github" / "workflows" / "crawler.yml").read_text(encoding="utf-8")
    found_crons = set(re.findall(r'cron:\s*["\']([^"\']+)', crawler_text))
    if found_crons != set(EXPECTED_CRONS):
        errors.append(f"crawler cron set differs from expected mapping: {sorted(found_crons)}")
    for cron, (slot, profile) in EXPECTED_CRONS.items():
        if f'"{cron}") slot={slot}; profile={profile}' not in crawler_text:
            errors.append(f"crawler workflow lacks explicit mapping for {cron} -> {slot}/{profile}")

    for workflow_name, secret_names in REQUIRED_WORKFLOW_SECRETS.items():
        workflow_path = ROOT / ".github" / "workflows" / workflow_name
        try:
            workflow_text = workflow_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"workflow {workflow_name}: {exc}")
            continue
        missing_secrets = sorted(
            name for name in secret_names if f"secrets.{name}" not in workflow_text
        )
        if missing_secrets:
            errors.append(
                f"workflow {workflow_name} lacks required Secret references: "
                + ", ".join(missing_secrets)
            )

    if config.get("source_digest"):
        warnings.append("source_digest is deprecated and ignored; remove it after one release")

    if args.check_env:
        missing = [name for name in REQUIRED_PRODUCTION_ENV if not os.environ.get(name, "").strip()]
        if missing:
            errors.append("missing required production environment values: " + ", ".join(missing))

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("work", "relax"), default="work")
    parser.add_argument("--slot", choices=("A", "B", "C"), default="A")
    parser.add_argument("--check-env", action="store_true")
    args = parser.parse_args()
    errors, warnings = validate(args)
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    print(f"validation: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
