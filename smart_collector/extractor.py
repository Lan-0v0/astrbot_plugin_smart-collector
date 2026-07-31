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
    ContentType.VIDEO: (".mp4", ".webm", ".mov", ".mkv", ".m4v", ".m3u8", ".mpd"),
    ContentType.AUDIO: (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"),
    ContentType.IMAGE: (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp"),
}

MIME_PREFIXES = {
    "video/": ContentType.VIDEO,
    "audio/": ContentType.AUDIO,
    "image/": ContentType.IMAGE,
    "text/": ContentType.TEXT,
}
VIDEO_MANIFEST_MIME_TYPES = {
    "application/dash+xml",
    "application/mpegurl",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
}

URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
SCRIPT_MEDIA_PATTERN = re.compile(
    r"""["']([^"']+\.(?:m3u8|mpd|mp4|webm|mov|mkv|m4v|mp3|wav|ogg|m4a|flac|aac|jpg|jpeg|png|gif|webp|avif|bmp)(?:\?[^"']*)?)["']""",
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
                    source_kind="direct",
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
        for element in soup.select(
            "iframe[src], iframe[data-src], iframe[data-lazy-src], embed[src], embed[data-src]"
        ):
            raw = element.get("src") or element.get("data-src") or element.get("data-lazy-src")
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
                try:
                    forced_type = ContentType(path_item.get("content_type", ""))
                except ValueError:
                    forced_type = None
                candidate = self._candidate_from_url(
                    urljoin(base_url, value),
                    requested,
                    title=path_item.get("title", ""),
                    forced_type=forced_type,
                )
                if candidate:
                    candidate.selector = ".".join(map(str, path_item.get("path", [])))
                    candidate.context_text = " ".join(map(str, path_item.get("path", [])))
                    candidate.source_kind = "json_profile"
                    candidates.append(candidate)
        if candidates:
            for candidate in candidates:
                candidate.referer = candidate.referer or base_url
            return self._dedupe(candidates), profile

        paths: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for path, value in self._walk_json(payload):
            if not isinstance(value, str):
                continue
            urls = URL_PATTERN.findall(value)
            leaf = str(path[-1]).lower() if path else ""
            forced_type = None
            if ContentType.VIDEO in requested and (
                "video" in leaf
                or leaf
                in {
                    "contenturl",
                    "content_url",
                    "playurl",
                    "play_url",
                    "hls",
                    "dash",
                    "manifest",
                }
            ):
                forced_type = ContentType.VIDEO
            if (
                forced_type is None
                and ContentType.IMAGE in requested
                and any(
                    marker in leaf
                    for marker in (
                        "image",
                        "img",
                        "cover",
                        "poster",
                        "thumbnail",
                        "artwork",
                        "original",
                    )
                )
            ):
                forced_type = ContentType.IMAGE
            if not urls and value.strip().startswith(("/", "./", "../")):
                urls = [urljoin(base_url, value.strip())]
            for url in urls:
                candidate = self._candidate_from_url(
                    urljoin(base_url, url), requested, forced_type=forced_type
                )
                if candidate:
                    candidate.selector = ".".join(map(str, path))
                    candidate.context_text = " ".join(map(str, path))
                    candidate.source_kind = "json"
                    candidates.append(candidate)
                    paths.append({"path": path, "content_type": candidate.content_type.value})
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
        for candidate in candidates:
            candidate.referer = candidate.referer or base_url
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
        if requested == (ContentType.TEXT,):
            text = self._main_text(soup)
            candidates = (
                [
                    Candidate(
                        ContentType.TEXT, text=text, title=title, selector="article, main, body"
                    )
                ]
                if text
                else []
            )
            return candidates, profile
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
                        candidate.context_text = f"{title} {candidate.attribute}".strip()
                        candidate.source_kind = "json_ld"
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
                        self._apply_element_metadata(candidate, element, title)
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
                forced_type = None
                payload_type = (
                    str(payload.get("@type", "")).lower() if isinstance(payload, dict) else ""
                )
                if leaf == "contenturl" and "video" in payload_type:
                    forced_type = ContentType.VIDEO
                elif leaf == "contenturl" and "image" in payload_type:
                    forced_type = ContentType.IMAGE
                candidate = self._candidate_from_url(value, requested, title, forced_type)
                if candidate:
                    candidate.selector = "script[type='application/ld+json']"
                    candidate.attribute = ".".join(map(str, path))
                    candidate.context_text = f"{title} {candidate.attribute}".strip()
                    candidate.source_kind = "json_ld"
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
            ("video[data-url]", "data-url", ContentType.VIDEO),
            ("video[data-video]", "data-video", ContentType.VIDEO),
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
            ("meta[property='og:image:url']", "content", ContentType.IMAGE),
            ("meta[property='og:image:secure_url']", "content", ContentType.IMAGE),
            ("meta[name='twitter:player:stream']", "content", ContentType.VIDEO),
            ("meta[itemprop='contentUrl']", "content", ContentType.VIDEO),
            ("link[rel='preload'][as='video']", "href", ContentType.VIDEO),
            ("meta[name='twitter:image']", "content", ContentType.IMAGE),
            ("meta[name='twitter:image:src']", "content", ContentType.IMAGE),
            ("link[rel='image_src']", "href", ContentType.IMAGE),
            ("link[rel='preload'][as='image']", "href", ContentType.IMAGE),
            ("a[href]", "href", None),
            ("source[src]", "src", None),
            ("source[data-src]", "data-src", None),
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
                    raw, srcset_width = self._best_srcset(raw)
                else:
                    srcset_width = 0
                url = urljoin(response.url, raw.strip())
                candidate = self._candidate_from_url(url, requested, title, forced_type)
                if candidate:
                    candidate.selector = selector
                    candidate.attribute = attribute
                    candidate.width = max(candidate.width, srcset_width)
                    self._apply_element_metadata(candidate, element, title)
                    candidates.append(candidate)
                    found = True
            if found:
                selector_profile.append({"selector": selector, "attribute": attribute})

        if ContentType.IMAGE in requested:
            for element in soup.select("[style*='background']")[:200]:
                style = str(element.get("style") or "")
                for raw in re.findall(r"url\((?:['\"]?)([^)'\"]+)", style, re.IGNORECASE):
                    candidate = self._candidate_from_url(
                        urljoin(response.url, raw.strip()), requested, title, ContentType.IMAGE
                    )
                    if candidate:
                        candidate.selector = "[style*='background']"
                        candidate.attribute = "style"
                        self._apply_element_metadata(candidate, element, title)
                        candidate.source_kind = "css_background"
                        candidates.append(candidate)

        if ContentType.TEXT in requested:
            text = self._main_text(soup)
            if text:
                candidates.append(
                    Candidate(
                        ContentType.TEXT, text=text, title=title, selector="article, main, body"
                    )
                )
        for candidate in candidates:
            candidate.referer = candidate.referer or response.url
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
            candidate.context_text = title
            candidate.source_kind = "script"
            candidates.append(candidate)
        return candidates

    @staticmethod
    def _best_srcset(value: str) -> tuple[str, int]:
        best_url = ""
        best_width = 0
        best_score = -1.0
        for index, entry in enumerate(value.split(",")):
            parts = entry.strip().split()
            if not parts:
                continue
            score = float(index)
            width = 0
            if len(parts) > 1:
                descriptor = parts[-1].lower()
                try:
                    if descriptor.endswith("w"):
                        width = int(descriptor[:-1])
                        score = width * 10.0
                    elif descriptor.endswith("x"):
                        score = float(descriptor[:-1]) * 1_000_000.0
                except ValueError:
                    pass
            if score >= best_score:
                best_url = parts[0]
                best_width = width
                best_score = score
        return best_url, best_width

    @staticmethod
    def _apply_element_metadata(candidate: Candidate, element: Any, page_title: str) -> None:
        parts = [
            page_title,
            str(element.get("alt") or ""),
            str(element.get("title") or ""),
            str(element.get("aria-label") or ""),
            " ".join(element.get("class") or [])
            if not isinstance(element.get("class"), str)
            else str(element.get("class")),
            str(element.get("id") or ""),
        ]
        figure = element.find_parent("figure")
        if figure:
            caption = figure.find("figcaption")
            if caption:
                parts.append(caption.get_text(" ", strip=True))
        parent_link = element.find_parent("a")
        if parent_link:
            parts.append(str(parent_link.get("title") or ""))
            parts.append(parent_link.get_text(" ", strip=True)[:500])
        heading = element.find_previous(["h1", "h2", "h3"])
        if heading:
            parts.append(heading.get_text(" ", strip=True)[:500])
        candidate.context_text = " ".join(dict.fromkeys(part for part in parts if part))[:2000]
        candidate.in_main_content = bool(
            element.find_parent(["article", "main"]) or element.find_parent(attrs={"role": "main"})
        )
        candidate.source_kind = AdaptiveExtractor._source_kind(candidate.selector)
        for attribute in ("width", "height"):
            raw = str(element.get(attribute) or "")
            match = re.fullmatch(r"\s*(\d{1,5})(?:px)?\s*", raw, re.IGNORECASE)
            if match:
                setattr(
                    candidate,
                    attribute,
                    max(getattr(candidate, attribute), int(match.group(1))),
                )

    @staticmethod
    def _source_kind(selector: str) -> str:
        if "og:image" in selector:
            return "open_graph"
        if "twitter:image" in selector:
            return "twitter_card"
        if "image_src" in selector:
            return "image_src"
        if "srcset" in selector:
            return "srcset"
        if selector.startswith("img"):
            return "image_element"
        if selector.startswith("a["):
            return "image_link"
        return "html"

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
        normalized = mime_type.split(";", 1)[0].strip().lower()
        if normalized in VIDEO_MANIFEST_MIME_TYPES:
            return ContentType.VIDEO
        for prefix, content_type in MIME_PREFIXES.items():
            if normalized.startswith(prefix):
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
        source_priority = {
            "direct": 100,
            "json_ld": 90,
            "json": 85,
            "json_profile": 85,
            "open_graph": 80,
            "image_src": 75,
            "srcset": 70,
            "twitter_card": 65,
            "image_link": 60,
            "image_element": 50,
            "script": 40,
            "css_background": 20,
        }
        seen: dict[tuple[str, str], Candidate] = {}
        result: list[Candidate] = []
        for candidate in candidates:
            identity = (candidate.content_type.value, candidate.url or candidate.text)
            existing = seen.get(identity)
            if existing:
                existing.width = max(existing.width, candidate.width)
                existing.height = max(existing.height, candidate.height)
                existing.in_main_content = existing.in_main_content or candidate.in_main_content
                existing.context_text = " ".join(
                    dict.fromkeys(
                        part for part in (existing.context_text, candidate.context_text) if part
                    )
                )[:2000]
                existing.referer = existing.referer or candidate.referer
                existing.title = existing.title or candidate.title
                if source_priority.get(candidate.source_kind, 0) > source_priority.get(
                    existing.source_kind, 0
                ):
                    existing.source_kind = candidate.source_kind
                    existing.selector = candidate.selector
                    existing.attribute = candidate.attribute
                continue
            seen[identity] = candidate
            result.append(candidate)
        return result
