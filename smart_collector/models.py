from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ContentType(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"
    TEXT = "text"


CONTENT_PRIORITY = (
    ContentType.VIDEO,
    ContentType.IMAGE,
    ContentType.AUDIO,
    ContentType.TEXT,
)

CONTENT_LABELS = {
    "视频": ContentType.VIDEO,
    "音频": ContentType.AUDIO,
    "图片": ContentType.IMAGE,
    "图像": ContentType.IMAGE,
    "插画": ContentType.IMAGE,
    "文字": ContentType.TEXT,
    "文本": ContentType.TEXT,
}


def normalize_time(value: Any) -> str:
    raw = str(value or "23:00").strip()
    try:
        hour, minute = (int(part) for part in raw.split(":"))
    except (TypeError, ValueError):
        return "23:00"
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return "23:00"
    return f"{hour:02d}:{minute:02d}"


def normalize_types(values: Any) -> tuple[ContentType, ...]:
    if not values:
        return (ContentType.VIDEO,)
    if isinstance(values, str) or not isinstance(values, (list, tuple, set)):
        values = [values]
    result: list[ContentType] = []
    for value in values:
        item = CONTENT_LABELS.get(str(value))
        if item is None:
            try:
                item = ContentType(str(value).lower())
            except ValueError:
                continue
        if item not in result:
            result.append(item)
    return tuple(item for item in CONTENT_PRIORITY if item in result) or (ContentType.VIDEO,)


def normalize_video_quality(value: Any) -> str:
    return "highest" if str(value).strip().lower() == "highest" else "lowest"


def _safe_number(value: Any, default: int | float, converter: type[int] | type[float]):
    try:
        converted = converter(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if isinstance(converted, float) and not math.isfinite(converted):
        return default
    return converted


@dataclass(slots=True)
class SourceConfig:
    key: str
    template: str
    name: str
    enabled: bool
    url: str
    content_types: tuple[ContentType, ...]
    command: str
    dedupe: int = 0
    video_quality: str = "lowest"
    cookies: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)
    image_to_pdf: bool = False
    compress: bool = False
    compression_password: str = ""
    forward_mode: str = "user"
    custom_qq: str = ""
    rate_limit: float = 1.0
    schedules: tuple[str, ...] = ()
    schedule_time: str = "23:00"
    target_qq_groups: tuple[str, ...] = ()
    pixiv_refresh_token: str = ""
    pixiv_age_mode: str = "all"

    @classmethod
    def from_mapping(cls, value: dict[str, Any], index: int = 0) -> SourceConfig:
        template = str(value.get("__template_key") or value.get("template") or "website")
        if template not in {"website", "api", "pixiv"}:
            template = "website"
        name = str(value.get("name") or f"未命名条目 {index + 1}").strip()
        command = str(value.get("command") or "").strip()
        if command and not command.startswith("/"):
            command = "/" + command

        headers: dict[str, str] = {}
        raw_headers = value.get("headers")
        if isinstance(raw_headers, dict):
            headers.update({str(k): str(v) for k, v in raw_headers.items() if k and v is not None})
        header_key = str(value.get("header_key") or "").strip()
        if header_key:
            headers[header_key] = str(value.get("header_value") or "")

        cookies = value.get("cookies") or []
        if isinstance(cookies, str) or not isinstance(cookies, (list, tuple, set)):
            cookies = [cookies]
        target_qq_groups = value.get("target_qq_groups") or []
        if isinstance(target_qq_groups, (str, int)):
            target_qq_groups = [target_qq_groups]
        schedules = value.get("schedules") or []
        if isinstance(schedules, str):
            schedules = [schedules]
        elif not isinstance(schedules, (list, tuple, set)):
            schedules = []
        dedupe = int(_safe_number(value.get("dedupe", 0), 0, int))
        dedupe = -1 if dedupe < 0 else 0
        rate_limit = float(_safe_number(value.get("rate_limit", 1.0), 1.0, float))
        forward_mode = str(value.get("forward_mode") or "user")
        if forward_mode not in {"none", "user", "custom"}:
            forward_mode = "user"

        pixiv_refresh_token = str(value.get("refresh_token") or "").strip()
        pixiv_age_mode = str(value.get("age_mode") or "all").strip().lower()
        if pixiv_age_mode not in {"all", "safe", "r18"}:
            pixiv_age_mode = "all"
        url = str(value.get("url") or "").strip()
        if template == "pixiv":
            url = url or "https://app-api.pixiv.net/"
            content_types = (ContentType.IMAGE,)
        else:
            content_types = normalize_types(value.get("content_types"))

        return cls(
            key=f"{template}:{name}:{hashlib.sha1(url.encode()).hexdigest()[:10]}",
            template=template,
            name=name,
            enabled=bool(value.get("enabled", True)),
            url=url,
            content_types=content_types,
            command=command,
            dedupe=dedupe,
            video_quality=normalize_video_quality(value.get("video_quality")),
            cookies=tuple(str(item).strip() for item in cookies if str(item).strip()),
            headers=headers,
            image_to_pdf=bool(value.get("image_to_pdf", False)),
            compress=bool(value.get("compress", False)),
            compression_password=str(value.get("compression_password") or ""),
            forward_mode=forward_mode,
            custom_qq=str(value.get("custom_qq") or ""),
            rate_limit=rate_limit,
            schedules=tuple(str(item) for item in schedules),
            schedule_time=normalize_time(value.get("schedule_time")),
            target_qq_groups=tuple(
                dict.fromkeys(
                    str(item).strip() for item in target_qq_groups if str(item).strip().isdigit()
                )
            ),
            pixiv_refresh_token=pixiv_refresh_token,
            pixiv_age_mode=pixiv_age_mode,
        )


@dataclass(slots=True)
class FetchResponse:
    url: str
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = "httpx"


@dataclass(slots=True)
class DownloadedFile:
    url: str
    status: int
    content_type: str
    headers: dict[str, str]
    local_path: Path
    sha256: str
    size: int
    transport: str = "httpx"


@dataclass(slots=True)
class Candidate:
    content_type: ContentType
    url: str = ""
    text: str = ""
    title: str = ""
    mime_type: str = ""
    selector: str = ""
    attribute: str = ""
    referer: str = ""
    width: int = 0
    height: int = 0
    context_text: str = ""
    source_kind: str = ""
    in_main_content: bool = False
    content_length: int = 0


@dataclass(slots=True)
class CollectedAsset:
    asset_key: str
    source_key: str
    source_name: str
    content_type: ContentType
    origin_url: str
    title: str = ""
    text: str = ""
    mime_type: str = ""
    local_path: Path | None = None
    cached: bool = False
    summary: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.local_path and self.local_path.exists())
