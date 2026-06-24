# coding=utf-8
"""Fetch, analyze, and archive the current day's AI Digest page.

Network footprint is intentionally tiny:
- request the RSS feed once;
- request the matching daily HTML page once;
- never download images or linked source pages.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import requests

from trendradar.ai.client import AIClient
from trendradar.core import load_config
from trendradar.context import AppContext
from trendradar.storage.base import RSSData, RSSItem


FEED_ID = "ai-digest-daily"
FEED_NAME = "AI Digest Daily"
DEFAULT_FEED_URL = "https://masiqi.github.io/ai-digest/feed.xml"
DEFAULT_BASE_URL = "https://masiqi.github.io/ai-digest/"
DEFAULT_PROMPT_FILE = "config/ai_digest_analysis_prompt.txt"
USER_AGENT = "RavenisCore/2.0 AI Digest Daily Archive (https://github.com/AplusNeutrino/DailyAna)"

SECTION_SENTENCE = "一句话"
SECTION_LINK = "链接"
SECTION_PLAYBOOK = "怎么玩"
SECTION_SIGNIFICANCE = "为什么值得关注"
SECTION_USE_CASES = "应用场景"


@dataclass
class DigestTopic:
    item_index: int = 0
    digest_id: str = ""
    title: str = ""
    page_url: str = ""
    primary_url: str = ""
    published_at: str = ""
    sections: Dict[str, List[str]] = field(default_factory=dict)
    link_urls: List[str] = field(default_factory=list)
    source_urls: List[str] = field(default_factory=list)
    clean_html: str = ""
    full_text: str = ""
    content_hash: str = ""

    def text_for(self, label: str) -> str:
        return "\n".join(v for v in self.sections.get(label, []) if v).strip()

    @property
    def sentence(self) -> str:
        return self.text_for(SECTION_SENTENCE)

    @property
    def link_text(self) -> str:
        values = list(self.sections.get(SECTION_LINK, []))
        values.extend(self.link_urls)
        return "\n".join(v for v in values if v).strip()

    @property
    def playbook(self) -> str:
        return self.text_for(SECTION_PLAYBOOK)

    @property
    def significance(self) -> str:
        return self.text_for(SECTION_SIGNIFICANCE)

    @property
    def use_cases(self) -> List[str]:
        return list(self.sections.get(SECTION_USE_CASES, []))

    def to_archive_dict(self) -> Dict[str, Any]:
        return {
            "date": self.published_at[:10],
            "item_index": self.item_index,
            "digest_id": self.digest_id,
            "title": self.title,
            "page_url": self.page_url,
            "primary_url": self.primary_url,
            "published_at": self.published_at,
            "sentence": self.sentence,
            "link_text": self.link_text,
            "playbook": self.playbook,
            "significance": self.significance,
            "use_cases": self.use_cases,
            "source_urls": self.source_urls,
            "full_text": self.full_text,
            "content_hash": self.content_hash,
        }


class DigestPageParser(HTMLParser):
    """Purpose-built parser for masiqi.github.io/ai-digest daily pages."""

    TRACKED_TAGS = {"div", "p", "ul", "li", "a"}

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.stack: List[set[str]] = []
        self.topics: List[DigestTopic] = []
        self.topic: Optional[DigestTopic] = None
        self.topic_stack_index: Optional[int] = None
        self.collecting: Optional[str] = None
        self.buffer: List[str] = []
        self.current_section_label = ""
        self.current_anchor_href = ""
        self.in_section = False
        self.in_sources = False
        self.in_usecase_list = False

    @staticmethod
    def _classes(attrs) -> set[str]:
        for key, value in attrs:
            if key == "class":
                return set((value or "").split())
        return set()

    @staticmethod
    def _href(attrs) -> str:
        for key, value in attrs:
            if key == "href":
                return value or ""
        return ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in self.TRACKED_TAGS:
            return

        classes = self._classes(attrs)
        self.stack.append(classes)

        if tag == "div" and "topic" in classes and self.topic is None:
            self.topic = DigestTopic(page_url=self.page_url)
            self.topic_stack_index = len(self.stack)

        if not self.topic:
            return

        if tag == "div":
            if "section" in classes:
                self.in_section = True
                self.current_section_label = ""
            elif "section-label" in classes:
                self.collecting = "section_label"
                self.buffer = []
            elif "topic-title" in classes:
                self.collecting = "topic_title"
                self.buffer = []
            elif "sources" in classes:
                self.in_sources = True
        elif tag == "p" and self.in_section:
            self.collecting = "section_text"
            self.buffer = []
        elif tag == "ul" and "usecase-list" in classes:
            self.in_usecase_list = True
        elif tag == "li" and self.in_usecase_list:
            self.collecting = "usecase_item"
            self.buffer = []
        elif tag == "a":
            self.current_anchor_href = urljoin(self.page_url, self._href(attrs))
            if self.current_section_label == SECTION_LINK and self.current_anchor_href:
                self.topic.link_urls.append(self.current_anchor_href)
                if not self.topic.primary_url:
                    self.topic.primary_url = self.current_anchor_href
            if self.in_sources and self.current_anchor_href:
                self.topic.source_urls.append(self.current_anchor_href)

    def handle_data(self, data: str) -> None:
        if self.collecting is not None:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.TRACKED_TAGS or not self.stack:
            return

        if self.topic:
            if tag == "div" and self.collecting == "topic_title":
                self.topic.title = self._clean("".join(self.buffer))
                self.collecting = None
            elif tag == "div" and self.collecting == "section_label":
                self.current_section_label = self._clean("".join(self.buffer))
                self.collecting = None
            elif tag == "p" and self.collecting == "section_text":
                text = self._clean("".join(self.buffer))
                if text and self.current_section_label:
                    self.topic.sections.setdefault(self.current_section_label, []).append(text)
                self.collecting = None
            elif tag == "li" and self.collecting == "usecase_item":
                text = self._clean("".join(self.buffer))
                if text:
                    self.topic.sections.setdefault(SECTION_USE_CASES, []).append(text)
                self.collecting = None
            elif tag == "ul" and self.in_usecase_list:
                self.in_usecase_list = False

        closing_classes = self.stack[-1]
        if tag == "div":
            if "section" in closing_classes:
                self.in_section = False
                self.current_section_label = ""
            if "sources" in closing_classes:
                self.in_sources = False
            if self.topic and self.topic_stack_index == len(self.stack):
                if self.topic.title:
                    self.topic.link_urls = list(dict.fromkeys(self.topic.link_urls))
                    self.topic.source_urls = list(dict.fromkeys(self.topic.source_urls))
                    self.topics.append(self.topic)
                self.topic = None
                self.topic_stack_index = None

        self.stack.pop()
        if tag == "a":
            self.current_anchor_href = ""

    @staticmethod
    def _clean(text: str) -> str:
        text = html.unescape(text or "")
        return re.sub(r"\s+", " ", text).strip()


def request_text(url: str, timeout: int) -> str:
    response = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/html;q=0.9, */*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def parse_feed_for_date(feed_xml: str, target_date: str, base_url: str) -> Optional[Dict[str, str]]:
    root = ET.fromstring(feed_xml)
    channel = root.find("channel")
    if channel is None:
        return None

    for item in channel.findall("item"):
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        pub_date_text = (item.findtext("pubDate") or "").strip()
        pub_date = ""
        if pub_date_text:
            try:
                pub_date = parsedate_to_datetime(pub_date_text).isoformat()
            except Exception:
                pub_date = pub_date_text

        if target_date in link or target_date.replace("-", "/") in title or target_date in pub_date:
            return {
                "title": title,
                "link": urljoin(base_url, link),
                "description": description,
                "published_at": pub_date,
            }
    return None


def expected_topic_count(page_html: str) -> int:
    count = 0
    for class_value in re.findall(r'<div\s+class=["\']([^"\']+)["\']', page_html):
        if "topic" in class_value.split():
            count += 1
    return count


def render_clean_html(topic: DigestTopic) -> str:
    parts = [f'<article class="ai-digest-topic" data-digest-id="{html.escape(topic.digest_id)}">']
    parts.append(f"<h2>{html.escape(topic.title)}</h2>")
    for label in (SECTION_SENTENCE, SECTION_LINK, SECTION_PLAYBOOK, SECTION_SIGNIFICANCE, SECTION_USE_CASES):
        values = topic.sections.get(label, [])
        if not values:
            continue
        parts.append(f'<section data-label="{html.escape(label)}">')
        parts.append(f"<h3>{html.escape(label)}</h3>")
        if label == SECTION_USE_CASES:
            parts.append("<ul>")
            for value in values:
                parts.append(f"<li>{html.escape(value)}</li>")
            parts.append("</ul>")
        else:
            for value in values:
                parts.append(f"<p>{html.escape(value)}</p>")
        if label == SECTION_LINK and topic.link_urls:
            parts.append("<ul>")
            for url in topic.link_urls:
                safe = html.escape(url)
                parts.append(f'<li><a href="{safe}">{safe}</a></li>')
            parts.append("</ul>")
        parts.append("</section>")
    if topic.source_urls:
        parts.append('<section data-label="原文链接"><h3>原文链接</h3><ul>')
        for url in topic.source_urls:
            safe = html.escape(url)
            parts.append(f'<li><a href="{safe}">{safe}</a></li>')
        parts.append("</ul></section>")
    parts.append("</article>")
    return "\n".join(parts)


def finalize_topics(topics: List[DigestTopic], digest_item: Dict[str, str], target_date: str) -> None:
    published_at = digest_item.get("published_at") or f"{target_date}T11:00:00+08:00"
    page_url = digest_item.get("link", "")

    for index, topic in enumerate(topics, 1):
        topic.item_index = index
        topic.page_url = page_url or topic.page_url
        topic.published_at = published_at
        if not topic.primary_url:
            topic.primary_url = topic.page_url
        digest_seed = f"{topic.title}{topic.primary_url}"
        topic.digest_id = f"{FEED_ID}:{target_date}:{index}:{hashlib.sha1(digest_seed.encode('utf-8')).hexdigest()[:16]}"

        text_parts = [
            topic.title,
            topic.sentence,
            topic.link_text,
            topic.playbook,
            topic.significance,
            "\n".join(topic.use_cases),
            "\n".join(topic.source_urls),
            topic.page_url,
        ]
        topic.full_text = "\n".join(part for part in text_parts if part).strip()
        topic.clean_html = render_clean_html(topic)
        hash_payload = "\n".join([
            topic.title,
            topic.sentence,
            topic.link_text,
            topic.playbook,
            topic.significance,
            "\n".join(topic.use_cases),
            "\n".join(topic.source_urls),
        ])
        topic.content_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()


def topic_to_summary(topic: DigestTopic) -> str:
    parts = []
    if topic.sentence:
        parts.append(f"{SECTION_SENTENCE}: {topic.sentence}")
    if topic.link_text:
        parts.append(f"{SECTION_LINK}: {topic.link_text}")
    if topic.playbook:
        parts.append(f"{SECTION_PLAYBOOK}: {topic.playbook}")
    if topic.significance:
        parts.append(f"{SECTION_SIGNIFICANCE}: {topic.significance}")
    if topic.use_cases:
        parts.append(f"{SECTION_USE_CASES}: " + " / ".join(topic.use_cases))
    if topic.page_url:
        parts.append(f"日报页: {topic.page_url}")
    if topic.source_urls:
        parts.append("原文链接: " + " ".join(topic.source_urls))
    return "\n".join(parts)


def build_rss_data(topics: List[DigestTopic], ctx: AppContext, target_date: str) -> RSSData:
    crawl_time = ctx.format_time()
    rss_items = [
        RSSItem(
            title=topic.title,
            feed_id=FEED_ID,
            feed_name=FEED_NAME,
            url=topic.primary_url or topic.page_url,
            guid=topic.digest_id,
            published_at=topic.published_at,
            summary=topic_to_summary(topic),
            author="masiqi/ai-digest",
            crawl_time=crawl_time,
            first_time=crawl_time,
            last_time=crawl_time,
            count=1,
        )
        for topic in topics
    ]
    return RSSData(
        date=target_date,
        crawl_time=crawl_time,
        items={FEED_ID: rss_items},
        id_to_name={FEED_ID: FEED_NAME},
        failed_ids=[],
    )


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def get_rss_connection(storage_manager, date: str) -> sqlite3.Connection:
    backend = storage_manager.get_backend()
    if not hasattr(backend, "_get_connection"):
        raise RuntimeError("storage backend does not expose SQLite connections")
    return backend._get_connection(date, db_type="rss")


def save_ai_digest_items(storage_manager, topics: List[DigestTopic], date: str, crawl_time: str) -> None:
    conn = get_rss_connection(storage_manager, date)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for topic in topics:
        conn.execute(
            """
            INSERT INTO ai_digest_items
            (date, item_index, digest_id, title, page_url, primary_url, published_at,
             sentence, link_text, playbook, significance, use_cases_json,
             source_urls_json, clean_html, full_text, content_hash,
             first_crawl_time, last_crawl_time, crawl_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(digest_id) DO UPDATE SET
                date = excluded.date,
                item_index = excluded.item_index,
                title = excluded.title,
                page_url = excluded.page_url,
                primary_url = excluded.primary_url,
                published_at = excluded.published_at,
                sentence = excluded.sentence,
                link_text = excluded.link_text,
                playbook = excluded.playbook,
                significance = excluded.significance,
                use_cases_json = excluded.use_cases_json,
                source_urls_json = excluded.source_urls_json,
                clean_html = excluded.clean_html,
                full_text = excluded.full_text,
                content_hash = excluded.content_hash,
                last_crawl_time = excluded.last_crawl_time,
                crawl_count = ai_digest_items.crawl_count + 1,
                updated_at = excluded.updated_at
            """,
            (
                date,
                topic.item_index,
                topic.digest_id,
                topic.title,
                topic.page_url,
                topic.primary_url,
                topic.published_at,
                topic.sentence,
                topic.link_text,
                topic.playbook,
                topic.significance,
                dumps(topic.use_cases),
                dumps(topic.source_urls),
                topic.clean_html,
                topic.full_text,
                topic.content_hash,
                crawl_time,
                crawl_time,
                now_str,
                now_str,
            ),
        )
    conn.commit()
    rebuild_ai_digest_fts(conn, date)


def normalize_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def save_analysis_skipped(storage_manager, topics: List[DigestTopic], date: str, model: str, reason: str) -> None:
    conn = get_rss_connection(storage_manager, date)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for topic in topics:
        conn.execute(
            """
            INSERT INTO ai_digest_item_analysis
            (digest_id, date, status, model, error, analyzed_at, updated_at)
            VALUES (?, ?, 'skipped', ?, ?, ?, ?)
            ON CONFLICT(digest_id) DO UPDATE SET
                status = excluded.status,
                summary = '',
                key_points_json = '[]',
                category = '',
                tags_json = '[]',
                entities_json = '[]',
                retrieval_keywords_json = '[]',
                model = excluded.model,
                error = excluded.error,
                raw_response = '',
                analyzed_at = excluded.analyzed_at,
                updated_at = excluded.updated_at
            """,
            (topic.digest_id, date, model, reason, now_str, now_str),
        )
    conn.execute(
        """
        INSERT INTO ai_digest_daily_analysis
        (date, status, model, error, analyzed_at, updated_at)
        VALUES (?, 'skipped', ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            status = excluded.status,
            summary = '',
            theme_clusters_json = '[]',
            notable_items_json = '[]',
            overall_observation = '',
            model = excluded.model,
            error = excluded.error,
            raw_response = '',
            analyzed_at = excluded.analyzed_at,
            updated_at = excluded.updated_at
        """,
        (date, model, reason, now_str, now_str),
    )
    conn.commit()
    rebuild_ai_digest_fts(conn, date)


def save_analysis_failed(storage_manager, topics: List[DigestTopic], date: str, model: str, error: str, raw_response: str = "") -> None:
    conn = get_rss_connection(storage_manager, date)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for topic in topics:
        conn.execute(
            """
            INSERT INTO ai_digest_item_analysis
            (digest_id, date, status, model, error, raw_response, analyzed_at, updated_at)
            VALUES (?, ?, 'failed', ?, ?, ?, ?, ?)
            ON CONFLICT(digest_id) DO UPDATE SET
                status = excluded.status,
                summary = '',
                key_points_json = '[]',
                category = '',
                tags_json = '[]',
                entities_json = '[]',
                retrieval_keywords_json = '[]',
                model = excluded.model,
                error = excluded.error,
                raw_response = excluded.raw_response,
                analyzed_at = excluded.analyzed_at,
                updated_at = excluded.updated_at
            """,
            (topic.digest_id, date, model, error, raw_response, now_str, now_str),
        )
    conn.execute(
        """
        INSERT INTO ai_digest_daily_analysis
        (date, status, model, error, raw_response, analyzed_at, updated_at)
        VALUES (?, 'failed', ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            status = excluded.status,
            summary = '',
            theme_clusters_json = '[]',
            notable_items_json = '[]',
            overall_observation = '',
            model = excluded.model,
            error = excluded.error,
            raw_response = excluded.raw_response,
            analyzed_at = excluded.analyzed_at,
            updated_at = excluded.updated_at
        """,
        (date, model, error, raw_response, now_str, now_str),
    )
    conn.commit()
    rebuild_ai_digest_fts(conn, date)


def save_analysis_success(storage_manager, topics: List[DigestTopic], date: str, model: str, parsed: Dict[str, Any], raw_response: str) -> None:
    conn = get_rss_connection(storage_manager, date)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    by_id = {
        str(item.get("digest_id", "")): item
        for item in parsed.get("items", [])
        if isinstance(item, dict)
    }

    for topic in topics:
        item = by_id.get(topic.digest_id)
        if not item:
            conn.execute(
                """
                INSERT INTO ai_digest_item_analysis
                (digest_id, date, status, model, error, raw_response, analyzed_at, updated_at)
                VALUES (?, ?, 'failed', ?, 'missing item analysis in AI response', ?, ?, ?)
                ON CONFLICT(digest_id) DO UPDATE SET
                    status = excluded.status,
                    model = excluded.model,
                    error = excluded.error,
                    raw_response = excluded.raw_response,
                    analyzed_at = excluded.analyzed_at,
                    updated_at = excluded.updated_at
                """,
                (topic.digest_id, date, model, raw_response, now_str, now_str),
            )
            continue
        conn.execute(
            """
            INSERT INTO ai_digest_item_analysis
            (digest_id, date, status, summary, key_points_json, category, tags_json,
             entities_json, retrieval_keywords_json, model, error, raw_response,
             analyzed_at, updated_at)
            VALUES (?, ?, 'success', ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            ON CONFLICT(digest_id) DO UPDATE SET
                status = excluded.status,
                summary = excluded.summary,
                key_points_json = excluded.key_points_json,
                category = excluded.category,
                tags_json = excluded.tags_json,
                entities_json = excluded.entities_json,
                retrieval_keywords_json = excluded.retrieval_keywords_json,
                model = excluded.model,
                error = excluded.error,
                raw_response = excluded.raw_response,
                analyzed_at = excluded.analyzed_at,
                updated_at = excluded.updated_at
            """,
            (
                topic.digest_id,
                date,
                str(item.get("summary", "")).strip(),
                dumps(normalize_list(item.get("key_points"))),
                str(item.get("category", "")).strip(),
                dumps(normalize_list(item.get("tags"))),
                dumps(normalize_list(item.get("entities"))),
                dumps(normalize_list(item.get("retrieval_keywords"))),
                model,
                raw_response,
                now_str,
                now_str,
            ),
        )

    daily = parsed.get("daily") if isinstance(parsed.get("daily"), dict) else {}
    conn.execute(
        """
        INSERT INTO ai_digest_daily_analysis
        (date, status, summary, theme_clusters_json, notable_items_json,
         overall_observation, model, error, raw_response, analyzed_at, updated_at)
        VALUES (?, 'success', ?, ?, ?, ?, ?, '', ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            status = excluded.status,
            summary = excluded.summary,
            theme_clusters_json = excluded.theme_clusters_json,
            notable_items_json = excluded.notable_items_json,
            overall_observation = excluded.overall_observation,
            model = excluded.model,
            error = excluded.error,
            raw_response = excluded.raw_response,
            analyzed_at = excluded.analyzed_at,
            updated_at = excluded.updated_at
        """,
        (
            date,
            str(daily.get("summary", "")).strip(),
            dumps(daily.get("theme_clusters", [])),
            dumps(daily.get("notable_items", [])),
            str(daily.get("overall_observation", "")).strip(),
            model,
            raw_response,
            now_str,
            now_str,
        ),
    )
    conn.commit()
    rebuild_ai_digest_fts(conn, date)


def json_array_text(value: Any) -> str:
    try:
        parsed = json.loads(value or "[]")
        if isinstance(parsed, list):
            return " ".join(str(item) for item in parsed)
    except Exception:
        pass
    return ""


def rebuild_ai_digest_fts(conn: sqlite3.Connection, date: str) -> None:
    rows = conn.execute(
        """
        SELECT i.digest_id, i.title, i.sentence, i.link_text, i.playbook,
               i.significance, i.use_cases_json, i.source_urls_json, i.full_text,
               a.summary, a.tags_json, a.retrieval_keywords_json
        FROM ai_digest_items i
        LEFT JOIN ai_digest_item_analysis a ON a.digest_id = i.digest_id
        WHERE i.date = ?
        """,
        (date,),
    ).fetchall()
    for row in rows:
        digest_id = row["digest_id"] if isinstance(row, sqlite3.Row) else row[0]
        conn.execute("DELETE FROM ai_digest_items_fts WHERE digest_id = ?", (digest_id,))
        conn.execute(
            """
            INSERT INTO ai_digest_items_fts
            (digest_id, title, sentence, link_text, playbook, significance,
             use_cases, source_urls, full_text, analysis_summary,
             analysis_tags, analysis_keywords)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["digest_id"],
                row["title"] or "",
                row["sentence"] or "",
                row["link_text"] or "",
                row["playbook"] or "",
                row["significance"] or "",
                json_array_text(row["use_cases_json"]),
                json_array_text(row["source_urls_json"]),
                row["full_text"] or "",
                row["summary"] or "",
                json_array_text(row["tags_json"]),
                json_array_text(row["retrieval_keywords_json"]),
            ),
        )
    conn.commit()


def build_analysis_payload(topics: List[DigestTopic], date: str) -> Dict[str, Any]:
    return {
        "date": date,
        "source": FEED_NAME,
        "items": [topic.to_archive_dict() for topic in topics],
    }


def parse_ai_json(response: str) -> Dict[str, Any]:
    text = (response or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            from json_repair import repair_json

            return json.loads(repair_json(text))
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
            raise


def run_ai_analysis(storage_manager, config: Dict[str, Any], topics: List[DigestTopic], date: str, no_ai: bool, prompt_file: str) -> None:
    ai_config = config.get("AI", {})
    model = ai_config.get("MODEL", "")
    client = AIClient(ai_config)

    if no_ai:
        save_analysis_skipped(storage_manager, topics, date, model, "AI analysis disabled by --no-ai")
        print("[ai-digest] AI analysis skipped by --no-ai")
        return
    if not client.api_key:
        save_analysis_skipped(storage_manager, topics, date, model, "AI API key is not configured")
        print("[ai-digest] AI analysis skipped: AI API key is not configured")
        return

    prompt_path = Path(prompt_file)
    if not prompt_path.exists():
        raise FileNotFoundError(f"AI Digest prompt file not found: {prompt_path}")

    payload = build_analysis_payload(topics, date)
    prompt_template = prompt_path.read_text(encoding="utf-8")
    user_prompt = prompt_template.replace("{payload}", json.dumps(payload, ensure_ascii=False, indent=2))
    messages = [
        {
            "role": "system",
            "content": "You analyze daily AI/product news and return strict JSON only.",
        },
        {"role": "user", "content": user_prompt},
    ]

    raw_response = ""
    try:
        raw_response = client.chat(messages, temperature=0.2)
        parsed = parse_ai_json(raw_response)
        if not isinstance(parsed.get("items"), list) or not isinstance(parsed.get("daily"), dict):
            raise ValueError("AI response must contain items[] and daily{}")
        save_analysis_success(storage_manager, topics, date, model, parsed, raw_response)
        print("[ai-digest] AI analysis saved")
    except Exception as exc:
        save_analysis_failed(storage_manager, topics, date, model, f"{type(exc).__name__}: {exc}", raw_response)
        print(f"[ai-digest] AI analysis failed but archive is preserved: {exc}")


def write_json_snapshot(topics: List[DigestTopic], digest_item: Dict[str, str], date: str) -> Path:
    output_dir = Path("output") / "external" / FEED_ID
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{date}.json"
    payload = {
        "feed_id": FEED_ID,
        "feed_name": FEED_NAME,
        "date": date,
        "daily_page": digest_item.get("link", ""),
        "count": len(topics),
        "topics": [topic.to_archive_dict() for topic in topics],
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and archive today's masiqi AI Digest topics once.")
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--date", help="Target date in YYYY-MM-DD. Defaults to configured timezone today.")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--json-snapshot", action="store_true", help="Also write output/external JSON for inspection.")
    parser.add_argument("--no-ai", action="store_true", help="Archive content but skip AI analysis.")
    parser.add_argument("--prompt-file", default=DEFAULT_PROMPT_FILE)
    args = parser.parse_args()

    config = load_config()
    ctx = AppContext(config)
    storage = ctx.get_storage_manager()
    batch_started = False
    try:
        target_date = args.date or ctx.format_date()
        print(f"[ai-digest] checking feed for {target_date}: {args.feed_url}")
        feed_xml = request_text(args.feed_url, args.timeout)
        digest_item = parse_feed_for_date(feed_xml, target_date, args.base_url)
        if not digest_item:
            print(f"[ai-digest] today's digest is not available yet: {target_date}")
            return 0

        daily_url = digest_item["link"]
        print(f"[ai-digest] fetching daily page once: {daily_url}")
        page_html = request_text(daily_url, args.timeout)
        expected_count = expected_topic_count(page_html)
        parser_obj = DigestPageParser(daily_url)
        parser_obj.feed(page_html)
        topics = parser_obj.topics

        if not topics:
            print("[ai-digest] no topic cards found; archive aborted")
            return 1
        if expected_count and len(topics) != expected_count:
            print(f"[ai-digest] parsed topic count mismatch: parsed={len(topics)} expected={expected_count}")
            return 1

        finalize_topics(topics, digest_item, target_date)
        crawl_time = ctx.format_time()

        storage.begin_batch()
        batch_started = True
        save_ai_digest_items(storage, topics, target_date, crawl_time)
        print(f"[ai-digest] archived {len(topics)} complete topics")

        rss_data = build_rss_data(topics, ctx, target_date)
        if not storage.save_rss_data(rss_data):
            print("[ai-digest] failed to save RSS compatibility data")
            return 1
        print(f"[ai-digest] saved {len(topics)} RSS compatibility items as {FEED_ID}")

        run_ai_analysis(storage, config, topics, target_date, args.no_ai, args.prompt_file)

        if args.json_snapshot:
            snapshot = write_json_snapshot(topics, digest_item, target_date)
            print(f"[ai-digest] wrote snapshot: {snapshot}")

        return 0
    finally:
        if batch_started:
            storage.end_batch()
        ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
