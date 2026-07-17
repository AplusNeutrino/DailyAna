#!/usr/bin/env python3
"""Build a Ravenis weekly synthesis from published daily history shards."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from trendradar.ai import AIAnalyzer
from trendradar.core import load_config, parse_multi_account_config
from trendradar.intelligence import build_digest_summary
from trendradar.notification.wework import send_wework_messages
from trendradar.weekly import build_weekly_digest, render_weekly_markdown, weekly_intelligence_package


def read_records(history_dir: Path) -> list[dict]:
    records = []
    for path in sorted((history_dir / "days").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        records.extend(payload.get("items", []) or [])
    return records


def weekly_detail_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["view"] = "weekly"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the deterministic Ravenis weekly digest")
    parser.add_argument("--history-dir", default="docs/history")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--output", default="docs/history/weekly/latest.json")
    parser.add_argument("--markdown-output", default="docs/history/weekly/latest.md")
    parser.add_argument("--detail-url", default="")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--ai", action="store_true", help="Use one bounded AI call over at most 12 themes")
    parser.add_argument("--send", action="store_true", help="Send the weekly Markdown to configured WeCom webhooks")
    args = parser.parse_args()
    records = read_records(Path(args.history_dir))
    digest = build_weekly_digest(records, end_date=args.end_date)
    config = None
    detail_url = args.detail_url
    if args.ai or args.send:
        config = load_config(args.config)
        intelligence_config = config.get("INTELLIGENCE_PUSH", {})
        if not detail_url:
            detail_url = (intelligence_config.get("wechat") or {}).get("detail_url", "")
        if args.ai:
            analyzer = AIAnalyzer(config.get("AI", {}), config.get("AI_ANALYSIS", {}), datetime.now)
            package = weekly_intelligence_package(digest)
            result = analyzer.analyze_intelligence_package(
                package,
                intelligence_config.get("summary") or {},
                report_type="每周情报摘要",
            )
            digest["editorial_summary"] = build_digest_summary(
                package,
                result.digest_summary if result.success else None,
                intelligence_config,
            ).to_dict()
    else:
        package = weekly_intelligence_package(digest)
        digest["editorial_summary"] = build_digest_summary(package).to_dict()
    detail_url = weekly_detail_url(detail_url) if detail_url else ""
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(digest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    markdown = Path(args.markdown_output)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    message = render_weekly_markdown(digest, detail_url)
    markdown.write_text(message, encoding="utf-8")
    if args.send:
        webhooks = parse_multi_account_config(config.get("WEWORK_WEBHOOK_URL", "")) if config else []
        if not webhooks:
            raise RuntimeError("weekly send requested but no WeCom webhook is configured")
        if not send_wework_messages(webhooks, [message], msg_type="markdown", max_bytes=4000):
            raise RuntimeError("weekly WeCom send failed")
    print(f"[weekly] records={digest['record_count']} themes={digest['theme_count']} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
