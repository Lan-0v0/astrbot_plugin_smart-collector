from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .models import CONTENT_LABELS, ContentType, SourceConfig

DEFAULT_SUMMARY_PROMPT = (
    "请把下面爬取到的文字提炼为准确、精简、易读的中文摘要。保留关键事实、数字、"
    "专有名词和必要链接，不推测原文未提供的信息；直接输出摘要，不要写前言。"
)
HTTP_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)


def load_sources(config: dict[str, Any]) -> list[SourceConfig]:
    values = config.get("custom_sources") or []
    result: list[SourceConfig] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        source = SourceConfig.from_mapping(value, index)
        if source.url:
            result.append(source)
    return result


def requested_types(text: str, allowed: tuple[ContentType, ...]) -> tuple[ContentType, ...]:
    selected: list[ContentType] = []
    lowered = text.lower()
    for label, content_type in CONTENT_LABELS.items():
        if (
            (label in text or content_type.value in lowered)
            and content_type in allowed
            and content_type not in selected
        ):
            selected.append(content_type)
    return tuple(item for item in allowed if item in selected) if selected else allowed


def split_url_request(text: str) -> tuple[str, str]:
    match = HTTP_URL_PATTERN.search(text)
    if not match:
        return "", text.strip()
    url = match.group(0).rstrip(".,;:!?\"')]}，。；：！？）】》")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "", text.strip()
    remainder = f"{text[: match.start()]} {text[match.start() + len(url) :]}"
    remainder = " ".join(remainder.split()).strip(".,;:!?，。；：！？")
    return url, remainder.strip()
