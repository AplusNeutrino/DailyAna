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
    "5 6 * * *": ("B", "general"),
    "10 9 * * *": ("C", "relax"),
}
EXPECTED_AI_DIGEST_CRONS = {"0 2 * * *"}
EXPECTED_SLOT_TIMES = {"A": "06:00", "B": "14:05", "C": "17:10"}
SCHEDULED_PROFILES = ("work", "general", "relax")
REQUIRED_PRODUCTION_ENV = (
    "WEWORK_WEBHOOK_URL",
    "WEWORK_MSG_TYPE",
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
    for slot_id, expected_time in EXPECTED_SLOT_TIMES.items():
        actual_time = str((slots.get(slot_id) or {}).get("time", ""))
        if actual_time != expected_time:
            errors.append(
                f"slot {slot_id} time differs from production schedule: "
                f"expected {expected_time}, got {actual_time or 'missing'}"
            )

    for label, items in (
        ("platform", (config.get("platforms") or {}).get("sources") or []),
        ("RSS", (config.get("rss") or {}).get("feeds") or []),
    ):
        duplicates = duplicate_ids(items)
        if duplicates:
            errors.append(f"duplicate {label} IDs: {', '.join(duplicates)}")

    rss_config = config.get("rss") or {}
    rss_feeds = rss_config.get("feeds") or []
    rsshub_base_url = str(rss_config.get("rsshub_base_url") or "").strip()
    if rsshub_base_url and not re.fullmatch(r"https?://\S+", rsshub_base_url):
        errors.append("rss.rsshub_base_url must be an HTTP(S) URL")
    expected_feed_count = rss_config.get("expected_feed_count")
    if expected_feed_count is not None:
        try:
            expected_feed_count = int(expected_feed_count)
        except (TypeError, ValueError):
            errors.append("rss.expected_feed_count must be an integer")
        else:
            if len(rss_feeds) != expected_feed_count:
                errors.append(
                    "RSS feed count differs from configured inventory: "
                    f"expected {expected_feed_count}, got {len(rss_feeds)}"
                )

    rss_urls: dict[str, str] = {}
    for feed in rss_feeds:
        if not isinstance(feed, dict):
            continue
        feed_id = str(feed.get("id") or "")
        url = str(feed.get("url") or "").strip()
        if not re.fullmatch(r"https?://\S+", url):
            errors.append(f"RSS feed {feed_id or '<missing>'} has invalid HTTP(S) URL")
            continue
        normalized_url = url.rstrip("/").lower()
        previous = rss_urls.get(normalized_url)
        if previous and previous != feed_id:
            errors.append(f"duplicate RSS URL shared by {previous} and {feed_id}")
        else:
            rss_urls[normalized_url] = feed_id

    categories_path = Path(private_dir) / "content_categories.yaml" if private_dir else Path()
    if not private_dir or not categories_path.exists():
        categories_path = ROOT / "config" / "content_categories.yaml"
    try:
        category_map = (read_yaml(categories_path).get("categories") or {})
        selected = (config.get("content") or {}).get("selected_categories") or []
        dangling = sorted(set(selected) - set(category_map))
        if dangling:
            errors.append(f"unknown selected content categories: {', '.join(dangling)}")

        known_sources = {
            "platforms": {
                str(item.get("id"))
                for item in (config.get("platforms") or {}).get("sources") or []
                if isinstance(item, dict) and item.get("id")
            },
            "rss_feeds": {
                str(item.get("id"))
                for item in rss_feeds
                if isinstance(item, dict) and item.get("id")
            },
        }
        for field, label in (("platforms", "platform"), ("rss_feeds", "RSS")):
            referenced = {
                str(source_id)
                for category in category_map.values()
                if isinstance(category, dict)
                for source_id in category.get(field, []) or []
            }
            unknown = sorted(referenced - known_sources[field])
            if unknown:
                errors.append(
                    f"content categories reference unknown {label} sources: "
                    + ", ".join(unknown)
                )

        profile_sources: dict[str, dict[str, set[str]]] = {}
        for profile_name in SCHEDULED_PROFILES:
            candidate = next(
                (
                    directory / f"{profile_name}.yaml"
                    for directory in profile_dirs
                    if (directory / f"{profile_name}.yaml").exists()
                ),
                None,
            )
            if candidate is None:
                errors.append(f"profile does not exist: {profile_name}")
                continue
            profile_data = read_yaml(candidate)
            category_ids = (profile_data.get("content") or {}).get("selected_categories") or []
            dangling_profile = sorted(set(category_ids) - set(category_map))
            if dangling_profile:
                errors.append(
                    f"profile {profile_name} references unknown content categories: "
                    + ", ".join(dangling_profile)
                )
                continue
            profile_sources[profile_name] = {
                field: {
                    str(source_id)
                    for category_id in category_ids
                    for source_id in (category_map.get(category_id) or {}).get(field, []) or []
                }
                for field in ("platforms", "rss_feeds")
            }

        if set(profile_sources) == set(SCHEDULED_PROFILES):
            for field, label in (("platforms", "platform"), ("rss_feeds", "RSS")):
                owners: dict[str, str] = {}
                for profile_name in SCHEDULED_PROFILES:
                    for source_id in profile_sources[profile_name][field]:
                        previous = owners.get(source_id)
                        if previous:
                            errors.append(
                                f"{label} source {source_id} overlaps profiles "
                                f"{previous} and {profile_name}"
                            )
                        else:
                            owners[source_id] = profile_name
                unassigned = sorted(known_sources[field] - set(owners))
                if unassigned:
                    errors.append(
                        f"configured {label} sources are not assigned to a profile: "
                        + ", ".join(unassigned)
                    )
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
        publisher_aliases = rules.get("publisher_aliases") or {}
        alias_owners: dict[str, str] = {}
        for canonical, aliases in publisher_aliases.items():
            for alias in [canonical, *(aliases or [])]:
                token = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(alias).lower())
                owner = alias_owners.get(token)
                if token and owner and owner != canonical:
                    errors.append(
                        f"publisher alias {alias!r} is shared by {owner!r} and {canonical!r}"
                    )
                elif token:
                    alias_owners[token] = str(canonical)
    except Exception as exc:
        errors.append(f"intelligence rules: {exc}")

    crawler_text = (ROOT / ".github" / "workflows" / "crawler.yml").read_text(encoding="utf-8")
    found_crons = set(re.findall(r'cron:\s*["\']([^"\']+)', crawler_text))
    if found_crons != set(EXPECTED_CRONS):
        errors.append(f"crawler cron set differs from expected mapping: {sorted(found_crons)}")
    for cron, (slot, profile) in EXPECTED_CRONS.items():
        if f'"{cron}") slot={slot}; profile={profile}' not in crawler_text:
            errors.append(f"crawler workflow lacks explicit mapping for {cron} -> {slot}/{profile}")

    ai_digest_text = (
        ROOT / ".github" / "workflows" / "ai-digest-daily.yml"
    ).read_text(encoding="utf-8")
    ai_digest_crons = set(re.findall(r'cron:\s*["\']([^"\']+)', ai_digest_text))
    if ai_digest_crons != EXPECTED_AI_DIGEST_CRONS:
        errors.append(
            "AI Digest cron set differs from expected schedule: "
            f"{sorted(ai_digest_crons)}"
        )

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
        configured_msg_type = (
            os.environ.get("WEWORK_MSG_TYPE")
            or (((config.get("notification") or {}).get("channels") or {}).get("wework") or {}).get("msg_type")
            or "markdown"
        ).strip().lower()
        if configured_msg_type != "markdown":
            errors.append(
                "production WEWORK_MSG_TYPE must be markdown for Ravenis editorial links and hierarchy"
            )

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=SCHEDULED_PROFILES, default="work")
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
