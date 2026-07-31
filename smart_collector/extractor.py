from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import Candidate, ContentType, FetchResponse

EXTENSIONS = {
    ContentType.VIDEO: (".mp4", ".webm", ".mov", ".mkv", ".m3u8"),
    ContentType.AUDIO: (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"),
    ContentType.IMAGE: (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"),
}

MIME_PREFIXES = {
    "video/": ContentType.VIDEO,
    "audio/": ContentType.AUDIO,
    "image/": ContentType.IMAGE,
    "text/": ContentType.TEXT,
}

URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)


class AdaptiveExtractor:
    def extract(
        self,
        response: FetchResponse,
        requested: tuple[ContentType, ...],
        profile: dict[str, Any] | None = None,
    ) -> tuple[list[Candidate], dict[str, Any]]:
        profile = profile or {}
        direct_type = self._mime_type(response.content_type)
        if direct_type in requested and direct_type is not ContentType.TEXT:
            return [
                Candidate(
                    content_type=direct_type,
                    url=response.url,
                    title=self._filename(response.url),
                    mime_type=response.content_type,
                    selector="__response__",
                )
            ], {"mode": "direct", "content_type": direct_type.value}

        if "json" in response.content_type or self._looks_json(response.body):
            try:
                payload = json.loads(response.body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            if payload is not None:
                return self._extract_json(payload, response.url, requested, profile)

        return self._extract_html(response, requested, profile)

    @staticmethod
    def extract_links(response: FetchResponse, limit: int = 24) -> list[str]:
        if "html" not in response.content_type:
            return []
        soup = BeautifulSoup(response.body.decode("utf-8", errors="replace"), "lxml")
        base_host = urlsplit(response.url).hostname
        links: list[str] = []
        for element in soup.select("a[href]"):
            href = element.get("href")
            if not isinstance(href, str):
                continue
            url = urljoin(response.url, href)
            parts = urlsplit(url)
            if (
                parts.scheme in {"http", "https"}
                and parts.hostname == base_host
                and url not in links
            ):
                links.append(url)
        links.sort(key=lambda item: "/movie/" not in urlsplit(item).path)
        return links[:limit]

    def _extract_json(
        self,
        payload: Any,
        base_url: str,
        requested: tuple[ContentType, ...],
        profile: dict[str, Any],
    ) -> tuple[list[Candidate], dict[str, Any]]:
        candidates: list[Candidate] = []
        known_paths = profile.get("json_paths") if profile.get("mode") == "json" else []
        for path_item in known_paths or []:
            value = self._get_json_path(payload, path_item.get("path", []))
            if isinstance(value, str):
                candidate = self._candidate_from_url(
                    value, requested, title=path_item.get("title", "")
                )
                if candidate:
                    candidate.selector = ".".join(map(str, path_item.get("path", [])))
                    candidates.append(candidate)
        if candidates:
            return self._dedupe(candidates), profile

        paths: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for path, value in self._walk_json(payload):
            if not isinstance(value, str):
                continue
            urls = URL_PATTERN.findall(value)
            for url in urls:
                candidate = self._candidate_from_url(url, requested)
                if candidate:
                    candidate.selector = ".".join(map(str, path))
                    candidates.append(candidate)
                    paths.append({"path": path, "content_type": candidate.content_type.value})
            leaf = str(path[-1]).lower() if path else ""
            if (
                ContentType.TEXT in requested
                and leaf in {"title", "text", "content", "description", "desc", "caption"}
                and value.strip()
                and not urls
            ):
                text_parts.append(value.strip())

        if text_parts and ContentType.TEXT in requested:
            candidates.append(
                Candidate(ContentType.TEXT, text="\n\n".join(text_parts), title="API 文本")
            )
        return self._dedupe(candidates), {"mode": "json", "json_paths": paths[:50]}

    def _extract_html(
        self,
        response: FetchResponse,
        requested: tuple[ContentType, ...],
        profile: dict[str, Any],
    ) -> tuple[list[Candidate], dict[str, Any]]:
        encoding = self._encoding(response)
        soup = BeautifulSoup(response.body.decode(encoding, errors="replace"), "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        candidates: list[Candidate] = []

        if profile.get("mode") == "html":
            for script in soup.select("script[type='application/ld+json']"):
                try:
                    payload = json.loads(script.get_text())
                except (json.JSONDecodeError, TypeError):
                    continue
                for item in profile.get("json_ld_paths", []):
                    value = self._get_json_path(payload, item.get("path", []))
                    if not isinstance(value, str):
                        continue
                    forced_type = ContentType(item["content_type"])
                    candidate = self._candidate_from_url(value, requested, title, forced_type)
                    if candidate:
                        candidate.selector = "script[type='application/ld+json']"
                        candidate.attribute = ".".join(map(str, item.get("path", [])))
                        candidates.append(candidate)
            for item in profile.get("selectors", []):
                try:
                    elements = soup.select(item["selector"])
                except Exception:
                    continue
                for element in elements[:100]:
                    value = element.get(item.get("attribute", "src"))
                    candidate = self._candidate_from_url(
                        urljoin(response.url, value or ""), requested, title
                    )
                    if candidate:
                        candidate.selector = item["selector"]
                        candidate.attribute = item.get("attribute", "src")
                        candidates.append(candidate)
        if candidates:
            return self._dedupe(candidates), profile

        selector_profile: list[dict[str, str]] = []
        json_ld_paths: list[dict[str, Any]] = []
        for script in soup.select("script[type='application/ld+json']"):
            try:
                payload = json.loads(script.get_text())
            except (json.JSONDecodeError, TypeError):
                continue
            for path, value in self._walk_json(payload):
                if not isinstance(value, str) or not value.startswith(("http://", "https://")):
                    continue
                leaf = str(path[-1]).lower() if path else ""
                forced_type = ContentType.VIDEO if leaf == "contenturl" else None
                candidate = self._candidate_from_url(value, requested, title, forced_type)
                if candidate:
                    candidate.selector = "script[type='application/ld+json']"
                    candidate.attribute = ".".join(map(str, path))
                    candidates.append(candidate)
                    json_ld_paths.append(
                        {"path": path, "content_type": candidate.content_type.value}
                    )

        rules = (
            ("video[src]", "src", ContentType.VIDEO),
            ("video source[src]", "src", ContentType.VIDEO),
            ("audio[src]", "src", ContentType.AUDIO),
            ("audio source[src]", "src", ContentType.AUDIO),
            ("img[data-original]", "data-original", ContentType.IMAGE),
            ("img[data-src]", "data-src", ContentType.IMAGE),
            ("img[src]", "src", ContentType.IMAGE),
            ("meta[property='og:video']", "content", ContentType.VIDEO),
            ("meta[property='og:video:url']", "content", ContentType.VIDEO),
            ("meta[property='og:audio']", "content", ContentType.AUDIO),
            ("meta[property='og:image']", "content", ContentType.IMAGE),
            ("a[href]", "href", None),
            ("source[src]", "src", None),
        )
        for selector, attribute, forced_type in rules:
            if forced_type and forced_type not in requested:
                continue
            found = False
            for element in soup.select(selector)[:300]:
                raw = element.get(attribute, "")
                if not isinstance(raw, str) or not raw.strip():
                    continue
                url = urljoin(response.url, raw.strip())
                candidate = self._candidate_from_url(url, requested, title, forced_type)
                if candidate:
                    candidate.selector = selector
                    candidate.attribute = attribute
                    candidates.append(candidate)
                    found = True
            if found:
                selector_profile.append({"selector": selector, "attribute": attribute})

        if ContentType.TEXT in requested:
            text = self._main_text(soup)
            if text:
                candidates.append(
                    Candidate(
                        ContentType.TEXT, text=text, title=title, selector="article, main, body"
                    )
                )
        return self._dedupe(candidates), {
            "mode": "html",
            "selectors": selector_profile,
            "json_ld_paths": json_ld_paths,
        }

    @staticmethod
    def _main_text(soup: BeautifulSoup) -> str:
        for element in soup.select("script, style, noscript, nav, footer, header, aside"):
            element.decompose()
        root = soup.select_one("article") or soup.select_one("main") or soup.body
        if not root:
            return ""
        blocks = [
            item.get_text(" ", strip=True) for item in root.select("h1, h2, h3, p, li, blockquote")
        ]
        blocks = [item for item in blocks if len(item) >= 2]
        if not blocks:
            blocks = [root.get_text("\n", strip=True)]
        text = "\n".join(dict.fromkeys(blocks))
        return text[:200_000]

    @staticmethod
    def _walk_json(value: Any, path: list[Any] | None = None) -> Iterable[tuple[list[Any], Any]]:
        path = path or []
        if isinstance(value, dict):
            for key, item in value.items():
                yield from AdaptiveExtractor._walk_json(item, [*path, key])
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from AdaptiveExtractor._walk_json(item, [*path, index])
        else:
            yield path, value

    @staticmethod
    def _get_json_path(value: Any, path: list[Any]) -> Any:
        current = value
        try:
            for item in path:
                current = current[item]
            return current
        except (KeyError, IndexError, TypeError):
            return None

    def _candidate_from_url(
        self,
        url: str,
        requested: tuple[ContentType, ...],
        title: str = "",
        forced_type: ContentType | None = None,
    ) -> Candidate | None:
        if not url.startswith(("http://", "https://")):
            return None
        content_type = forced_type or self._url_type(url)
        if content_type is None or content_type not in requested:
            return None
        return Candidate(content_type=content_type, url=url, title=title or self._filename(url))

    @staticmethod
    def _url_type(url: str) -> ContentType | None:
        path = urlsplit(url).path.lower()
        for content_type, extensions in EXTENSIONS.items():
            if path.endswith(extensions):
                return content_type
        return None

    @staticmethod
    def _mime_type(mime_type: str) -> ContentType | None:
        for prefix, content_type in MIME_PREFIXES.items():
            if mime_type.startswith(prefix):
                return content_type
        return None

    @staticmethod
    def _filename(url: str) -> str:
        return urlsplit(url).path.rsplit("/", 1)[-1]

    @staticmethod
    def _looks_json(body: bytes) -> bool:
        return body.lstrip()[:1] in {b"{", b"["}

    @staticmethod
    def _encoding(response: FetchResponse) -> str:
        header = response.headers.get("content-type", "")
        match = re.search(r"charset=([\w-]+)", header, re.IGNORECASE)
        return match.group(1) if match else "utf-8"

    @staticmethod
    def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
        seen: set[tuple[str, str]] = set()
        result: list[Candidate] = []
        for candidate in candidates:
            identity = (candidate.content_type.value, candidate.url or candidate.text)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(candidate)
        return result
