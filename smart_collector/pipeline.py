from __future__ import annotations

import asyncio
import hashlib
import math
import mimetypes
import random
import re
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image, UnidentifiedImageError

from .cache import CacheStore
from .extractor import VIDEO_MANIFEST_MIME_TYPES, AdaptiveExtractor
from .fetcher import AntiBotFetcher, FetchError
from .models import (
    CONTENT_PRIORITY,
    Candidate,
    CollectedAsset,
    ContentType,
    FetchResponse,
    SourceConfig,
)
from .postprocess import PostProcessor


class CollectionError(RuntimeError):
    pass


class RateLimitError(CollectionError):
    def __init__(self, remaining: float) -> None:
        self.remaining = max(0.0, remaining)
        super().__init__(f"请求过快，请在 {self.remaining:.1f} 秒后重试")


IMAGE_UI_PATTERN = re.compile(
    r"(?:^|[/_.\-\s])(icon|logo|avatar|emoji|badge|sprite|favicon|tracker|pixel)(?:[/_.\-\s]|$)",
    re.IGNORECASE,
)
IMAGE_AD_PATTERN = re.compile(
    r"(?:^|[/_.\-\s])(ad|ads|advert|banner|promo|sponsor)(?:[/_.\-\s]|$)",
    re.IGNORECASE,
)
IMAGE_THUMB_PATTERN = re.compile(r"(?:thumb|thumbnail|small|tiny|preview)", re.IGNORECASE)
IMAGE_ORIGINAL_PATTERN = re.compile(r"(?:original|orig|full|master|large|hires|raw)", re.IGNORECASE)
IMAGE_QUERY_STOPWORDS = {
    "图片",
    "图像",
    "照片",
    "插画",
    "爬取",
    "抓取",
    "采集",
    "一张",
    "一些",
    "一个",
    "高清",
    "原图",
}


class CollectorPipeline:
    def __init__(
        self,
        data_dir: Path,
        *,
        image_ignore_size_kb: int = 100,
        concurrency: int = -1,
        timeout: float = -1,
        rng: random.Random | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.image_min_bytes = -1 if image_ignore_size_kb < 0 else image_ignore_size_kb * 1024
        self.cache = CacheStore(data_dir)
        self.fetcher = AntiBotFetcher(timeout=timeout, concurrency=concurrency)
        self.extractor = AdaptiveExtractor()
        self.postprocessor = PostProcessor(data_dir / "output")
        self._rng = rng or random.SystemRandom()
        self._rate_lock = asyncio.Lock()
        self._last_requests: dict[tuple[str, str], float] = {}

    async def initialize(self) -> None:
        await self.cache.initialize()

    async def close(self) -> None:
        await self.fetcher.close()
        await self.cache.close()

    async def collect_many(
        self,
        sources: list[SourceConfig],
        requested: tuple[ContentType, ...] | None,
        user_key: str,
        query: str = "",
    ) -> tuple[SourceConfig, CollectedAsset]:
        enabled = [source for source in sources if source.enabled]
        if not enabled:
            raise CollectionError("没有已启用的自定义爬取项")
        results = await asyncio.gather(
            *(self.collect(source, requested, user_key, query) for source in enabled),
            return_exceptions=True,
        )
        successful: list[tuple[SourceConfig, CollectedAsset]] = []
        errors: list[str] = []
        for source, result in zip(enabled, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"{source.name}: {result}")
            else:
                successful.append((source, result))
        if not successful:
            raise CollectionError("；".join(errors) or "没有抓取到可发送内容")
        successful.sort(key=lambda item: CONTENT_PRIORITY.index(item[1].content_type))
        return successful[0]

    async def collect(
        self,
        source: SourceConfig,
        requested: tuple[ContentType, ...] | None,
        user_key: str,
        query: str = "",
    ) -> CollectedAsset:
        await self._check_rate(source, user_key)
        allowed_types = tuple(
            item for item in CONTENT_PRIORITY if item in (requested or source.content_types)
        )
        allowed_types = tuple(item for item in allowed_types if item in source.content_types)
        if not allowed_types:
            raise CollectionError(f"{source.name} 未配置所请求的内容类型")

        asset: CollectedAsset | None = None
        try:
            direct_type = self.extractor._url_type(source.url)
            direct_mime = ""
            source_probe = getattr(self.fetcher, "probe", None)
            if (
                direct_type is None
                and allowed_types in {(ContentType.VIDEO,), (ContentType.IMAGE,)}
                and callable(source_probe)
            ):
                probe = await source_probe(
                    source.url, headers=AntiBotFetcher.source_headers(source)
                )
                if probe and 200 <= probe.status < 300:
                    direct_type = self.extractor._mime_type(probe.content_type)
                    direct_mime = probe.content_type
            if direct_type in allowed_types:
                asset = await self._try_candidates(
                    source,
                    FetchResponse(source.url, 200, "", b""),
                    [
                        Candidate(
                            direct_type,
                            url=source.url,
                            mime_type=direct_mime,
                            selector="__source_probe__" if direct_mime else "",
                        )
                    ],
                    query,
                )
            if asset is None:
                response = await self.fetcher.fetch_source(source)
                if response.status >= 400:
                    raise FetchError(f"HTTP {response.status}")
                if source.template == "website":
                    page_url = self.extractor.random_page_url(response, self._rng)
                    if page_url and page_url != response.url:
                        try:
                            paged = await self.fetcher.fetch(
                                page_url, headers=AntiBotFetcher.source_headers(source)
                            )
                            if paged.status < 400:
                                response = paged
                        except Exception:
                            pass
                profile = await self.cache.get_profile(source.key)
                candidates, new_profile = self.extractor.extract(response, allowed_types, profile)
                if new_profile and new_profile != profile:
                    await self.cache.save_profile(source.key, new_profile)
                asset = await self._collect_by_priority(
                    source, response, candidates, allowed_types, query
                )
        except Exception as exc:
            cached = await self._cached_fallback(source, allowed_types, query)
            if cached:
                asset = cached
            else:
                raise CollectionError(f"{source.name} 抓取失败: {exc}") from exc

        assert asset is not None
        if source.image_to_pdf and asset.content_type is ContentType.IMAGE:
            asset = await self.postprocessor.image_to_pdf(asset)
        if source.compress and asset.content_type in {ContentType.IMAGE, ContentType.VIDEO}:
            asset = await self.postprocessor.compress(asset, source.compression_password)
        return asset

    async def mark_sent(
        self, source: SourceConfig, asset: CollectedAsset, cache_days: int = 7
    ) -> None:
        base_key = asset.asset_key.split(":pdf", 1)[0].split(":zip", 1)[0]
        await self.cache.mark_sent(source.key, base_key)
        if cache_days == 0:
            await self.cache.cleanup({source.key: 0})

    async def cleanup(self, sources: list[SourceConfig], cache_days: int = 7) -> int:
        return await self.cache.cleanup_all(cache_days)

    async def _collect_by_priority(
        self,
        source: SourceConfig,
        source_response: FetchResponse,
        candidates: list[Candidate],
        allowed_types: tuple[ContentType, ...],
        query: str = "",
    ) -> CollectedAsset:
        detail_candidates: list[Candidate] | None = None
        for content_type in CONTENT_PRIORITY:
            if content_type not in allowed_types:
                continue
            asset = await self._try_candidates(
                source,
                source_response,
                [item for item in candidates if item.content_type is content_type],
                query,
            )
            if asset:
                return asset
            if source.template == "website":
                if detail_candidates is None:
                    detail_candidates = await self._crawl_detail_pages(
                        source, source_response, allowed_types
                    )
                asset = await self._try_candidates(
                    source,
                    source_response,
                    [item for item in detail_candidates if item.content_type is content_type],
                    query,
                )
                if asset:
                    return asset
            if content_type is ContentType.VIDEO:
                asset = await self._try_candidates(
                    source,
                    source_response,
                    await self._yt_dlp_candidates(
                        source_response.url,
                        source.video_quality,
                        AntiBotFetcher.source_headers(source),
                    ),
                    query,
                )
                if asset:
                    return asset

        cached = await self._cached_fallback(source, allowed_types, query)
        if cached:
            return cached
        raise CollectionError("解析到了页面，但没有符合类型及去重规则的可发送内容")

    async def _try_candidates(
        self,
        source: SourceConfig,
        source_response: FetchResponse,
        candidates: list[Candidate],
        query: str = "",
    ) -> CollectedAsset | None:
        pending = list(candidates)
        if pending and all(item.content_type is ContentType.IMAGE for item in pending):
            pending.sort(key=lambda item: self._image_candidate_score(item, query, 0), reverse=True)
        else:
            self._rng.shuffle(pending)
        for offset in range(0, min(len(pending), 300), 100):
            batch = await self._prioritize_candidates(source, pending[offset : offset + 100], query)
            for candidate in batch:
                direct_dynamic = candidate.selector == "__response__"
                if candidate.url and not direct_dynamic:
                    cached = await self.cache.get_asset_by_origin(source.key, candidate.url)
                    if (
                        cached
                        and await self.cache.is_allowed(source.key, cached.asset_key, source.dedupe)
                        and await self._image_asset_matches(cached, candidate, query)
                    ):
                        return cached
                try:
                    asset = await self._materialize(source, candidate, source_response)
                except Exception:
                    continue
                if await self.cache.is_allowed(
                    source.key, asset.asset_key, source.dedupe
                ) and await self._image_asset_matches(asset, candidate, query):
                    return asset
        return None

    async def _prioritize_candidates(
        self, source: SourceConfig, candidates: list[Candidate], query: str = ""
    ) -> list[Candidate]:
        probe = getattr(self.fetcher, "probe", None)
        if not callable(probe) or not candidates:
            return candidates
        gate = asyncio.Semaphore(12)

        def quality_order(candidate: Candidate, size: int) -> tuple[int, int, int]:
            pixels = candidate.width * candidate.height
            if not pixels and candidate.height:
                pixels = candidate.height * candidate.height
            quality_enabled = candidate.content_type is ContentType.VIDEO
            prefer_highest = quality_enabled and source.video_quality == "highest"
            quality_rank = (-pixels if prefer_highest else pixels) if quality_enabled else 0
            size_rank = -size if prefer_highest else size
            return int(quality_enabled and not pixels), quality_rank, size_rank

        async def inspect(index: int, candidate: Candidate) -> tuple[int, int, int, int, int]:
            if candidate.selector in {"__response__", "__source_probe__"}:
                return (0, 1, 0, 0, index)
            if not candidate.url or candidate.content_type is ContentType.TEXT:
                return (1, 1, 0, 0, index)
            async with gate:
                response = await probe(
                    candidate.url, headers=self._candidate_headers(source, candidate)
                )
                if (
                    response is not None
                    and response.status in {401, 403}
                    and candidate.referer
                    and urlsplit(candidate.url).hostname != urlsplit(source.url).hostname
                ):
                    response = await probe(
                        candidate.url,
                        headers=self._candidate_headers(
                            source, candidate, include_cross_origin_referer=True
                        ),
                    )
            if response is None:
                return (2, *quality_order(candidate, 0), index)
            try:
                size = int(response.headers.get("content-length", "0") or 0)
            except (TypeError, ValueError):
                size = 0
            actual_type = self.extractor._mime_type(response.content_type)
            if candidate.content_type is ContentType.IMAGE:
                score = self._image_candidate_score(candidate, query, size)
                below_size_limit = self.image_min_bytes >= 0 and 0 < size < self.image_min_bytes
                if 200 <= response.status < 300 and actual_type is ContentType.IMAGE:
                    return (int(below_size_limit), 0, -score, -size, index)
                if 200 <= response.status < 300:
                    return (1, 0, -score, -size, index)
                return (2, 0, -score, -size, index)
            quality = quality_order(candidate, size)
            if 200 <= response.status < 300 and actual_type is candidate.content_type:
                return (0, *quality, index)
            if 200 <= response.status < 300:
                return (1, *quality, index)
            if response.status in {401, 403, 405, 429}:
                return (2, *quality, index)
            return (3, *quality, index)

        inspection_limit = (
            30
            if candidates and all(item.content_type is ContentType.IMAGE for item in candidates)
            else 100
        )
        inspected = await asyncio.gather(
            *(
                inspect(index, candidate)
                for index, candidate in enumerate(candidates[:inspection_limit])
            )
        )
        ranked = sorted(
            zip(inspected, candidates[:inspection_limit], strict=True), key=lambda item: item[0]
        )
        return [candidate for _, candidate in ranked] + candidates[inspection_limit:]

    @classmethod
    def _image_candidate_score(cls, candidate: Candidate, query: str, size: int) -> int:
        source_scores = {
            "direct": 120,
            "json_ld": 100,
            "json": 90,
            "json_profile": 90,
            "open_graph": 85,
            "image_src": 80,
            "srcset": 75,
            "twitter_card": 70,
            "image_link": 65,
            "image_element": 45,
            "script": 40,
            "css_background": 20,
        }
        score = source_scores.get(candidate.source_kind, 30)
        if candidate.in_main_content:
            score += 55
        searchable = unquote(
            " ".join(
                (
                    candidate.url,
                    candidate.title,
                    candidate.context_text,
                    candidate.selector,
                )
            )
        ).casefold()
        if IMAGE_UI_PATTERN.search(searchable):
            score -= 180
        if IMAGE_AD_PATTERN.search(searchable):
            score -= 260
        if IMAGE_THUMB_PATTERN.search(searchable):
            score -= 55
        if IMAGE_ORIGINAL_PATTERN.search(searchable):
            score += 35

        pixels = candidate.width * candidate.height
        if pixels:
            score += min(70, max(0, int(math.log2(pixels)) * 4 - 45))
            ratio = max(candidate.width, candidate.height) / max(
                1, min(candidate.width, candidate.height)
            )
            if ratio > 8:
                score -= 65
        if size:
            score += min(35, int(math.log2(max(1, size // 1024) + 1) * 4))

        positive, negative, orientation, minimum = cls._image_query_requirements(query)
        for term in positive:
            if term in searchable:
                score += 35 if len(term) > 1 else 18
        for term in negative:
            if term in searchable:
                score -= 140
        if candidate.width and candidate.height:
            if orientation == "landscape":
                score += 25 if candidate.width > candidate.height else -100
            elif orientation == "portrait":
                score += 25 if candidate.height > candidate.width else -100
            elif orientation == "square":
                ratio = candidate.width / candidate.height
                score += 25 if 0.85 <= ratio <= 1.15 else -100
            if minimum and (candidate.width < minimum[0] or candidate.height < minimum[1]):
                score -= 150
        return score

    @staticmethod
    def _image_query_requirements(
        query: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], str, tuple[int, int] | None]:
        text = re.sub(r"https?://\S+", " ", query.casefold())
        resolution = re.search(r"(?<!\d)(\d{3,5})\s*[x×*]\s*(\d{3,5})(?!\d)", text)
        minimum = (int(resolution.group(1)), int(resolution.group(2))) if resolution else None
        orientation = ""
        if any(term in text for term in ("横屏", "横向", "landscape")):
            orientation = "landscape"
        elif any(term in text for term in ("竖屏", "竖向", "portrait")):
            orientation = "portrait"
        elif any(term in text for term in ("方图", "正方形", "square")):
            orientation = "square"

        negative = tuple(
            dict.fromkeys(
                match.group(1).strip("，,。.;； ")
                for match in re.finditer(r"(?:不要|排除|不含|没有)([\w\u4e00-\u9fff]{1,12})", text)
                if match.group(1)
            )
        )
        cleaned = re.sub(r"(?:不要|排除|不含|没有)[\w\u4e00-\u9fff]{1,12}", " ", text)
        cleaned = re.sub(r"\d{3,5}\s*[x×*]\s*\d{3,5}", " ", cleaned)
        for term in (*IMAGE_QUERY_STOPWORDS, "横屏", "横向", "竖屏", "竖向", "方图", "正方形"):
            cleaned = cleaned.replace(term, " ")
        chunks = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]+", cleaned)
        positive: list[str] = []
        for chunk in chunks:
            if chunk in IMAGE_QUERY_STOPWORDS:
                continue
            positive.append(chunk)
            if len(chunk) >= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
                positive.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
        return tuple(dict.fromkeys(positive)), negative, orientation, minimum

    async def _image_asset_matches(
        self, asset: CollectedAsset, candidate: Candidate, query: str
    ) -> bool:
        if candidate.content_type is not ContentType.IMAGE:
            return True
        if not asset.local_path or not asset.local_path.exists():
            return False
        try:
            if self.image_min_bytes >= 0 and asset.local_path.stat().st_size < self.image_min_bytes:
                return False
            width, height = await asyncio.to_thread(self._image_dimensions, asset.local_path)
        except (OSError, UnidentifiedImageError, ValueError):
            return False
        candidate.width = width
        candidate.height = height
        if width <= 8 or height <= 8 or width * height <= 256:
            return False
        _, _, orientation, minimum = self._image_query_requirements(query)
        if minimum and (width < minimum[0] or height < minimum[1]):
            return False
        if orientation == "landscape" and width <= height:
            return False
        if orientation == "portrait" and height <= width:
            return False
        return orientation != "square" or 0.85 <= width / height <= 1.15

    @staticmethod
    def _image_dimensions(path: Path) -> tuple[int, int]:
        with Image.open(path) as image:
            width, height = image.size
            image.verify()
        return width, height

    @staticmethod
    def _candidate_headers(
        source: SourceConfig,
        candidate: Candidate,
        *,
        include_cross_origin_referer: bool = False,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        same_origin = urlsplit(candidate.url).hostname == urlsplit(source.url).hostname
        if same_origin:
            headers.update(AntiBotFetcher.source_headers(source))
        if candidate.referer and (same_origin or include_cross_origin_referer):
            headers["Referer"] = candidate.referer
        return headers

    async def _materialize(
        self,
        source: SourceConfig,
        candidate: Candidate,
        source_response: FetchResponse,
    ) -> CollectedAsset:
        if (
            candidate.content_type is ContentType.VIDEO
            and candidate.url
            and (
                urlsplit(candidate.url).path.lower().endswith((".m3u8", ".mpd"))
                or candidate.mime_type.split(";", 1)[0].lower() in VIDEO_MANIFEST_MIME_TYPES
                or candidate.selector == "yt-dlp-download"
            )
        ):
            return await self._materialize_hls(source, candidate)
        if candidate.content_type is ContentType.TEXT:
            body = candidate.text.encode("utf-8")
            mime_type = "text/plain"
            origin_url = source_response.url
            content_digest = hashlib.sha256(body).hexdigest()
            target = self.cache.files_dir / f"{content_digest}.txt"
            if not target.exists():
                await asyncio.to_thread(target.write_bytes, body)
        elif candidate.selector == "__response__" and candidate.url == source_response.url:
            response = source_response
            if response.status >= 400:
                raise FetchError(f"资源返回 HTTP {response.status}")
            body = response.body
            mime_type = response.content_type or candidate.mime_type
            origin_url = candidate.url
            actual_type = self.extractor._mime_type(mime_type)
            if actual_type and actual_type is not candidate.content_type:
                raise CollectionError("资源响应类型与解析类型不一致")
            if "html" in mime_type or "json" in mime_type:
                raise CollectionError("候选资源不是可下载媒体")
            if (
                candidate.content_type is ContentType.IMAGE
                and self.image_min_bytes >= 0
                and len(body) < self.image_min_bytes
            ):
                raise FetchError(f"候选图片小于 {self.image_min_bytes // 1024} KB 忽略阈值")
            content_digest = hashlib.sha256(body).hexdigest()
            target = self.cache.files_dir / (
                content_digest + self._extension(origin_url, mime_type, candidate.content_type)
            )
            if not target.exists():
                await asyncio.to_thread(target.write_bytes, body)
        else:
            headers = self._candidate_headers(source, candidate)
            temporary = self.cache.files_dir / f".download-{uuid.uuid4().hex}"

            async def validate_download(downloaded) -> str:
                if downloaded.status >= 400:
                    raise FetchError(f"资源返回 HTTP {downloaded.status}")
                current_mime = downloaded.content_type or candidate.mime_type
                actual_type = self.extractor._mime_type(current_mime)
                if actual_type and actual_type is not candidate.content_type:
                    raise FetchError("资源响应类型与解析类型不一致")
                if "html" in current_mime or "json" in current_mime:
                    raise FetchError("候选资源不是可下载媒体")
                if candidate.content_type is ContentType.IMAGE:
                    actual_size = downloaded.local_path.stat().st_size
                    if self.image_min_bytes >= 0 and actual_size < self.image_min_bytes:
                        raise FetchError(f"候选图片小于 {self.image_min_bytes // 1024} KB 忽略阈值")
                    try:
                        width, height = await asyncio.to_thread(
                            self._image_dimensions, downloaded.local_path
                        )
                    except (OSError, UnidentifiedImageError, ValueError) as exc:
                        raise FetchError("候选图片文件无效") from exc
                    if width <= 8 or height <= 8 or width * height <= 256:
                        raise FetchError("候选图片是极小占位图")
                    candidate.width = width
                    candidate.height = height
                return current_mime

            try:
                try:
                    downloaded = await self.fetcher.download(
                        candidate.url, temporary, headers=headers
                    )
                    mime_type = await validate_download(downloaded)
                except FetchError:
                    if (
                        not candidate.referer
                        or urlsplit(candidate.url).hostname == urlsplit(source.url).hostname
                    ):
                        raise
                    downloaded = await self.fetcher.download(
                        candidate.url,
                        temporary,
                        headers=self._candidate_headers(
                            source, candidate, include_cross_origin_referer=True
                        ),
                    )
                    mime_type = await validate_download(downloaded)
                origin_url = candidate.url
                content_digest = downloaded.sha256
                target = self.cache.files_dir / (
                    content_digest + self._extension(origin_url, mime_type, candidate.content_type)
                )
                if target.exists():
                    temporary.unlink(missing_ok=True)
                else:
                    temporary.replace(target)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise

        digest = hashlib.sha256(f"{source.key}:{content_digest}".encode()).hexdigest()
        cached = await self.cache.get_asset(digest)
        if cached:
            if target != cached.local_path:
                target.unlink(missing_ok=True)
            return cached
        asset = CollectedAsset(
            asset_key=digest,
            source_key=source.key,
            source_name=source.name,
            content_type=candidate.content_type,
            origin_url=origin_url,
            title=candidate.title,
            text=candidate.text,
            mime_type=mime_type,
            local_path=target,
            cached=False,
        )
        await self.cache.save_asset(asset)
        return asset

    async def _materialize_hls(self, source: SourceConfig, candidate: Candidate) -> CollectedAsset:
        try:
            import yt_dlp
        except ImportError as exc:
            raise CollectionError("HLS 视频下载需要 yt-dlp") from exc

        partial_stem = self.cache.files_dir / f".hls-{uuid.uuid4().hex}"

        def download() -> Path:
            preferred_format = "best" if source.video_quality == "highest" else "worst"

            def cleanup() -> None:
                for partial in self.cache.files_dir.glob(partial_stem.name + ".*"):
                    partial.unlink(missing_ok=True)

            def run(format_selector: str | None, headers: dict[str, str]) -> Path:
                options = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "outtmpl": str(partial_stem) + ".%(ext)s",
                    "http_headers": headers,
                    "retries": 3,
                    "fragment_retries": 3,
                }
                if format_selector:
                    options["format"] = format_selector
                if self.fetcher.timeout is not None:
                    options["socket_timeout"] = self.fetcher.timeout
                with yt_dlp.YoutubeDL(options) as downloader:
                    info = downloader.extract_info(candidate.url, download=True)
                    prepared = Path(downloader.prepare_filename(info))
                if prepared.exists():
                    return prepared
                matches = list(self.cache.files_dir.glob(partial_stem.name + ".*"))
                if not matches:
                    raise CollectionError("yt-dlp 未生成视频文件")
                return matches[0]

            header_options = [self._candidate_headers(source, candidate)]
            cross_origin_headers = self._candidate_headers(
                source, candidate, include_cross_origin_referer=True
            )
            if cross_origin_headers != header_options[0]:
                header_options.append(cross_origin_headers)
            last_error: Exception | None = None
            for headers in header_options:
                for format_selector in (preferred_format, None):
                    cleanup()
                    try:
                        return run(format_selector, headers)
                    except Exception as exc:
                        last_error = exc
            cleanup()
            raise CollectionError(f"HLS 视频下载失败: {last_error}") from last_error

        try:
            downloaded = await asyncio.to_thread(download)
        except Exception:
            for partial in self.cache.files_dir.glob(partial_stem.name + ".*"):
                partial.unlink(missing_ok=True)
            raise
        try:
            content_digest = await asyncio.to_thread(self._file_digest, downloaded)
            asset_key = hashlib.sha256(f"{source.key}:{content_digest}".encode()).hexdigest()
            cached = await self.cache.get_asset(asset_key)
            if cached:
                downloaded.unlink(missing_ok=True)
                return cached
            suffix = downloaded.suffix.lower() or ".mp4"
            target = self.cache.files_dir / f"{content_digest}{suffix}"
            if target != downloaded:
                downloaded.replace(target)
            asset = CollectedAsset(
                asset_key=asset_key,
                source_key=source.key,
                source_name=source.name,
                content_type=ContentType.VIDEO,
                origin_url=candidate.url,
                title=candidate.title,
                mime_type=mimetypes.guess_type(target.name)[0] or "video/mp4",
                local_path=target,
                cached=False,
            )
            await self.cache.save_asset(asset)
            return asset
        except Exception:
            downloaded.unlink(missing_ok=True)
            raise

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    async def _cached_fallback(
        self,
        source: SourceConfig,
        allowed_types: tuple[ContentType, ...],
        query: str = "",
    ) -> CollectedAsset | None:
        assets = await self.cache.list_assets(source.key)
        for content_type in CONTENT_PRIORITY:
            if content_type not in allowed_types:
                continue
            for asset in assets:
                if (
                    asset.content_type is content_type
                    and await self.cache.is_allowed(source.key, asset.asset_key, source.dedupe)
                    and await self._image_asset_matches(
                        asset, Candidate(content_type=content_type), query
                    )
                ):
                    return asset
        return None

    async def _check_rate(self, source: SourceConfig, user_key: str) -> None:
        if source.rate_limit < 0:
            return
        key = (user_key, source.key)
        now = time.monotonic()
        async with self._rate_lock:
            previous = self._last_requests.get(key, 0.0)
            remaining = source.rate_limit - (now - previous)
            if remaining > 0:
                raise RateLimitError(remaining)
            self._last_requests[key] = now

    @staticmethod
    def _extension(url: str, mime_type: str, content_type: ContentType) -> str:
        path_suffix = Path(urlsplit(url).path).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,8}", path_suffix):
            return path_suffix
        guessed = mimetypes.guess_extension(mime_type) if mime_type else None
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
        return {
            ContentType.VIDEO: ".mp4",
            ContentType.AUDIO: ".mp3",
            ContentType.IMAGE: ".jpg",
            ContentType.TEXT: ".txt",
        }[content_type]

    @staticmethod
    async def _yt_dlp_candidates(
        url: str, video_quality: str, headers: dict[str, str] | None = None
    ) -> list[Candidate]:
        try:
            import yt_dlp
        except ImportError:
            return []

        def extract_with(format_selector: str | None) -> list[Candidate]:
            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": False,
                "http_headers": dict(headers or {}),
            }
            if format_selector:
                options["format"] = format_selector
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
            entries = info.get("entries") or [info]
            result: list[Candidate] = []
            for entry in entries:
                if not entry:
                    continue
                media_url = entry.get("url")
                if media_url:
                    result.append(
                        Candidate(
                            ContentType.VIDEO,
                            url=media_url,
                            title=entry.get("title") or "",
                            mime_type="video/mp4",
                            selector="yt-dlp",
                            referer=url,
                            width=int(entry.get("width") or 0),
                            height=int(entry.get("height") or 0),
                        )
                    )
            result.append(
                Candidate(
                    ContentType.VIDEO,
                    url=url,
                    title=info.get("title") or "",
                    selector="yt-dlp-download",
                    referer=url,
                )
            )
            return result

        try:
            preferred_format = "best" if video_quality == "highest" else "worst"
            try:
                return await asyncio.to_thread(extract_with, preferred_format)
            except Exception:
                return await asyncio.to_thread(extract_with, None)
        except Exception:
            return []

    async def _crawl_detail_pages(
        self,
        source: SourceConfig,
        response: FetchResponse,
        requested: tuple[ContentType, ...],
    ) -> list[Candidate]:
        links = self.extractor.extract_links(response)
        if not links:
            return []
        likely_details = [url for url in links if self.extractor.is_likely_detail_url(url)]
        links = likely_details or links
        self._rng.shuffle(links)
        links = links[:24]
        responses = await asyncio.gather(
            *(
                self.fetcher.fetch(url, headers=AntiBotFetcher.source_headers(source))
                for url in links
            ),
            return_exceptions=True,
        )
        candidates: list[Candidate] = []
        embed_links: dict[str, str] = {}
        for detail in responses:
            if isinstance(detail, BaseException) or detail.status >= 400:
                continue
            extracted, _ = self.extractor.extract(detail, requested)
            candidates.extend(extracted)
            for embed_url in self.extractor.extract_embed_links(detail):
                embed_links.setdefault(embed_url, detail.url)
        if not embed_links:
            return candidates

        def embed_score(item: tuple[str, str]) -> tuple[int, int]:
            url, _ = item
            path = urlsplit(url).path.lower()
            looks_like_player = any(marker in path for marker in ("/e/", "/embed/", "/player/"))
            looks_like_ad = any(
                marker in url.lower() for marker in ("/widgets/", "banner", "creative")
            )
            return (not looks_like_player, looks_like_ad)

        prioritized_embeds = sorted(embed_links.items(), key=embed_score)[:16]

        def embed_headers(url: str, referer: str) -> dict[str, str]:
            headers = {"Referer": referer}
            if urlsplit(url).hostname == urlsplit(source.url).hostname:
                headers.update(AntiBotFetcher.source_headers(source))
            return headers

        embedded = await asyncio.gather(
            *(
                self.fetcher.fetch(
                    url,
                    headers=embed_headers(url, referer),
                )
                for url, referer in prioritized_embeds
            ),
            return_exceptions=True,
        )
        for response_item in embedded:
            if isinstance(response_item, BaseException) or response_item.status >= 400:
                continue
            extracted, _ = self.extractor.extract(response_item, requested)
            candidates.extend(extracted)
        return candidates
