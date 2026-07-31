from __future__ import annotations

import json
import re
from collections.abc import Iterable
from contextlib import suppress
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from jsbeautifier.unpackers import packer

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
SCRIPT_MEDIA_PATTERN = re.compile(
    r"""["']([^"']+\.(?:m3u8|mp4|webm|mov|mkv|mp3|wav|ogg|m4a|flac|aac|jpg|jpeg|png|gif|webp|avif|bmp)(?:\?[^"']*)?)["']""",
    re.IGNORECASE,
)
PAGE_QUERY_KEYS = {"page", "paged", "p", "pg"}
DETAIL_PATH_MARKERS = ("/movie/", "/video/", "/watch/", "/post/", "/detail/", "/archives/")


class AdaptiveExtractor:
    def extract(
        self,
        response: FetchResponse,
        requested: tuple[ContentType, ...],
        profile: dict[str, Any] | None = None,
    ) -> tuple[list[Candidate], dict[str, Any]]:
        if not isinstance(profile, dict):
            profile = {}
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
    def extract_links(response: FetchResponse, limit: int = 100) -> list[str]:
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
        links.sort(key=lambda item: not AdaptiveExtractor.is_likely_detail_url(item))
        return links[:limit]

    @staticmethod
    def is_likely_detail_url(url: str) -> bool:
        path = urlsplit(url).path.lower()
        return any(marker in path for marker in DETAIL_PATH_MARKERS)

    @staticmethod
    def random_page_url(response: FetchResponse, rng: Any) -> str | None:
        """Infer a numeric pager and choose a random page while preserving its filters."""
        if "html" not in response.content_type:
            return None
        soup = BeautifulSoup(response.body.decode("utf-8", errors="replace"), "lxml")
        base_host = urlsplit(response.url).hostname
        query_pages: list[tuple[int, str, str]] = []
        path_pages: list[tuple[int, str, re.Match[str]]] = []
        for element in soup.select("a[href]"):
            href = element.get("href")
            if not isinstance(href, str):
                continue
            url = urljoin(response.url, href)
            parts = urlsplit(url)
            if parts.hostname != base_host:
                continue
            for key, value in parse_qsl(parts.query, keep_blank_values=True):
                if key.lower() in PAGE_QUERY_KEYS and value.isdigit() and int(value) > 0:
                    query_pages.append((int(value), url, key))
            match = re.search(r"(?i)(/page/)(\d+)(?=/|$)", parts.path)
            if match and int(match.group(2)) > 0:
                path_pages.append((int(match.group(2)), url, match))

        if query_pages:
            max_page, template, page_key = max(query_pages, key=lambda item: item[0])
            if max_page < 2:
                return None
            selected = rng.randint(1, max_page)
            parts = urlsplit(template)
            query = [
                (key, str(selected) if key == page_key else value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ]
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

        if path_pages:
            max_page, template, match = max(path_pages, key=lambda item: item[0])
            if max_page < 2:
                return None
            selected = rng.randint(1, max_page)
            parts = urlsplit(template)
            path = f"{parts.path[: match.start(2)]}{selected}{parts.path[match.end(2) :]}"
            return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
        return None

    @staticmethod
    def extract_embed_links(response: FetchResponse, limit: int = 16) -> list[str]:
        if "html" not in response.content_type:
            return []
        soup = BeautifulSoup(response.body.decode("utf-8", errors="replace"), "lxml")
        links: list[str] = []
        for element in soup.select("iframe[src], embed[src]"):
            raw = element.get("src")
            if not isinstance(raw, str) or not raw.strip():
                continue
            url = urljoin(response.url, raw.strip())
            parts = urlsplit(url)
            if parts.scheme in {"http", "https"} and parts.hostname and url not in links:
                links.append(url)

        def embed_score(url: str) -> tuple[int, int]:
            path = urlsplit(url).path.lower()
            looks_like_player = any(marker in path for marker in ("/e/", "/embed/", "/player/"))
            looks_like_ad = any(
                marker in url.lower() for marker in ("/widgets/", "banner", "creative")
            )
            return (not looks_like_player, looks_like_ad)

        links.sort(key=embed_score)
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
            if not isinstance(path_item, dict):
                continue
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
        candidates.extend(self._extract_script_media(soup, response.url, requested, title))

        if profile.get("mode") == "html":
            for script in soup.select("script[type='application/ld+json']"):
                try:
                    payload = json.loads(script.get_text())
                except (json.JSONDecodeError, TypeError):
                    continue
                for item in profile.get("json_ld_paths", []):
                    if not isinstance(item, dict):
                        continue
                    value = self._get_json_path(payload, item.get("path", []))
                    if not isinstance(value, str):
                        continue
                    try:
                        forced_type = ContentType(item.get("content_type", ""))
                    except ValueError:
                        continue
                    candidate = self._candidate_from_url(value, requested, title, forced_type)
                    if candidate:
                        candidate.selector = "script[type='application/ld+json']"
                        candidate.attribute = ".".join(map(str, item.get("path", [])))
                        candidates.append(candidate)
            for item in profile.get("selectors", []):
                if not isinstance(item, dict) or not isinstance(item.get("selector"), str):
                    continue
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
            ("video[data-src]", "data-src", ContentType.VIDEO),
            ("audio[data-src]", "data-src", ContentType.AUDIO),
            ("img[data-original]", "data-original", ContentType.IMAGE),
            ("img[data-src]", "data-src", ContentType.IMAGE),
            ("img[srcset]", "srcset", ContentType.IMAGE),
            ("img[src]", "src", ContentType.IMAGE),
            ("meta[property='og:video']", "content", ContentType.VIDEO),
            ("meta[property='og:video:url']", "content", ContentType.VIDEO),
            ("meta[property='og:video:secure_url']", "content", ContentType.VIDEO),
            ("meta[property='og:audio']", "content", ContentType.AUDIO),
            ("meta[property='og:image']", "content", ContentType.IMAGE),
            ("meta[name='twitter:player:stream']", "content", ContentType.VIDEO),
            ("meta[name='twitter:image']", "content", ContentType.IMAGE),
            ("a[href]", "href", None),
            ("source[src]", "src", None),
            ("source[srcset]", "srcset", None),
        )
        for selector, attribute, forced_type in rules:
            if forced_type and forced_type not in requested:
                continue
            found = False
            for element in soup.select(selector)[:300]:
                raw = element.get(attribute, "")
                if not isinstance(raw, str) or not raw.strip():
                    continue
                if attribute == "srcset":
                    entries = [item.strip().split()[0] for item in raw.split(",") if item.strip()]
                    raw = entries[-1] if entries else ""
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

    def _extract_script_media(
        self,
        soup: BeautifulSoup,
        base_url: str,
        requested: tuple[ContentType, ...],
        title: str,
    ) -> list[Candidate]:
        if not any(item in requested for item in EXTENSIONS):
            return []
        urls: list[str] = []
        for script in soup.select("script"):
            raw = script.get_text()
            sources: list[str] = []
            if packer.detect(raw):
                with suppress(Exception):
                    sources.append(packer.unpack(raw))
            else:
                sources.append(raw)
            for source in sources:
                for value in SCRIPT_MEDIA_PATTERN.findall(source):
                    url = urljoin(base_url, self._normalize_url(value))
                    if url not in urls:
                        urls.append(url)
        same_host = urlsplit(base_url).hostname
        urls.sort(key=lambda item: urlsplit(item).hostname != same_host)
        candidates: list[Candidate] = []
        for url in urls:
            candidate = self._candidate_from_url(url, requested, title)
            if candidate is None:
                continue
            candidate.selector = "script:media"
            candidate.attribute = "packed-or-inline"
            candidate.referer = base_url
            candidates.append(candidate)
        return candidates

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
        url = self._normalize_url(url)
        if not url.startswith(("http://", "https://")):
            return None
        content_type = forced_type or self._url_type(url)
        if content_type is None or content_type not in requested:
            return None
        width, height = self._url_resolution(url)
        return Candidate(
            content_type=content_type,
            url=url,
            title=title or self._filename(url),
            width=width,
            height=height,
        )

    @staticmethod
    def _normalize_url(url: str) -> str:
        value = unescape(str(url).strip())
        value = re.sub(r"\\u0026", "&", value, flags=re.IGNORECASE)
        value = re.sub(r"\\u003d", "=", value, flags=re.IGNORECASE)
        value = value.replace(r"\/", "/")
        return value.rstrip("\\")

    @staticmethod
    def _url_type(url: str) -> ContentType | None:
        path = urlsplit(url).path.lower()
        for content_type, extensions in EXTENSIONS.items():
            if path.endswith(extensions):
                return content_type
        return None

    @staticmethod
    def _url_resolution(url: str) -> tuple[int, int]:
        decoded = unescape(url)
        dimensions = re.search(r"(?<!\d)(\d{2,5})[xX](\d{2,5})(?!\d)", decoded)
        if dimensions:
            return int(dimensions.group(1)), int(dimensions.group(2))
        height = re.search(r"(?<!\d)(\d{3,4})p(?!\w)", decoded, re.IGNORECASE)
        return (0, int(height.group(1))) if height else (0, 0)

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
