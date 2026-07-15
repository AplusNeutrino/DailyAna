"""One byte-safe WeWork sender shared by crawler and AI Digest."""

from __future__ import annotations

import re
import time
from typing import Iterable, Optional

import requests


def split_utf8_text(text: str, max_bytes: int = 4000) -> list[str]:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    chunks: list[str] = []
    current = ""
    for line in str(text or "").splitlines(keepends=True) or [""]:
        for char in line:
            candidate = current + char
            if current and len(candidate.encode("utf-8")) > max_bytes:
                chunks.append(current.rstrip("\n"))
                current = char
            else:
                current = candidate
    if current or not chunks:
        chunks.append(current.rstrip("\n"))
    return [chunk for chunk in chunks if chunk]


def _plain_text(value: str) -> str:
    return re.sub(r"[*#>`]", "", value)


def send_wework_messages(
    webhook_urls: Iterable[str],
    messages: Iterable[str],
    *,
    msg_type: str = "markdown",
    max_bytes: int = 4000,
    interval: float = 1.0,
    proxy_url: Optional[str] = None,
    label: str = "企业微信",
) -> bool:
    urls = [url.strip() for url in webhook_urls if url and url.strip()]
    if not urls:
        print(f"{label}发送失败：未配置 webhook")
        return False
    text_mode = msg_type.lower() == "text"
    batches = []
    for message in messages:
        content = _plain_text(message) if text_mode else message
        batches.extend(split_utf8_text(content, max_bytes=max_bytes))
    if not batches:
        print(f"{label}发送失败：消息为空")
        return False
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    total = len(urls) * len(batches)
    sent = 0
    for account, webhook_url in enumerate(urls, 1):
        for index, content in enumerate(batches, 1):
            size = len(content.encode("utf-8"))
            if size > max_bytes:
                raise AssertionError(f"WeWork payload exceeds {max_bytes} bytes: {size}")
            payload = (
                {"msgtype": "text", "text": {"content": content}}
                if text_mode
                else {"msgtype": "markdown", "markdown": {"content": content}}
            )
            try:
                response = requests.post(
                    webhook_url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    proxies=proxies,
                    timeout=30,
                )
                result = response.json() if response.status_code == 200 else {}
                if response.status_code != 200 or result.get("errcode") != 0:
                    print(
                        f"{label}发送失败 account={account} batch={index}: "
                        f"HTTP {response.status_code} {result.get('errmsg', '')}"
                    )
                    return False
            except Exception as exc:
                print(f"{label}发送异常 account={account} batch={index}: {exc}")
                return False
            sent += 1
            print(f"{label}发送成功 {sent}/{total} ({size} bytes)")
            if sent < total and interval > 0:
                time.sleep(interval)
    return True
