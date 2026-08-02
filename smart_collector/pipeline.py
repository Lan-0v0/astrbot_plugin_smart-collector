from __future__ import annotations

import asyncio
import hashlib
import math
import mimetypes
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

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
from .pixiv import PixivCollector, PixivError
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
        self.pixiv = PixivCollector(data_dir)
        self._rng = rng or random.SystemRandom()
        self._rate_lock = asyncio.Lock()
        self._last_requests: dict[tuple[str, str], float] = {}
        self._pixiv_selection_lock = asyncio.Lock()
        self._pixiv_reserved: dict[str, set[str]] = {}
        self._collection_condition = asyncio.Condition()
        self._active_collections = 0
        self._closing = False

    async def initialize(self) -> None:
        await self.cache.initialize()

    async def close(self) -> None:
        async with self._collection_condition:
            self._closing = True
            await self._collection_condition.wait_for(lambda: self._active_collections == 0)
        await self.fetcher.close()
        await self.cache.close()

    @asynccontextmanager
    async def _collection_scope(self):
        async with self._collection_condition:
            if self._closing:
                raise CollectionError("Smart Collector 正在重新加载，请稍后重试")
            self._active_collections += 1
        try:
            yield
        finally:
            async with self._collection_condition:
                self._active_collections -= 1
                self._collection_condition.notify_all()

    async def collect_many(
        self,
        sources: list[SourceConfig],
        requested: tuple[ContentType, ...] | None,
        user_key: str,
        query: str = "",
    ) -> tuple[SourceConfig, CollectedAsset]:
        async with self._collection_scope():
            return await self._collect_many(sources, requested, user_key, query)

    async def _collect_many(
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
            *(self._collect(source, requested, user_key, query) for source in enabled),
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
            if len(errors) == 1:
                raise CollectionError(str(results[0]))
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
        async with self._collection_scope():
            return await self._collect(source, requested, user_key, query)

    async def _collect(
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

        if source.template == "pixiv":
            try:
                candidates = await self.pixiv.candidates(source, query)
                asset = await self._collect_pixiv_work(source, candidates, query)
                if asset is None:
                    raise PixivError("Pixiv 搜索结果均已发送过，请更换 Tag 后重试")
            except PixivError as exc:
                raise CollectionError(str(exc)) from exc
            if (
                source.image_to_pdf or (source.pixiv_r18_to_pdf and asset.r18)
            ) and asset.content_type is ContentType.IMAGE:
                asset = await self.postprocessor.image_to_pdf(asset)
            if source.compress and asset.content_type in {ContentType.IMAGE, ContentType.VIDEO}:
                asset = await self.postprocessor.compress(asset, source.compression_password)
            return asset

        asset: CollectedAsset | None = None
        try:
            direct_type = self.extractor._url_type(source.url)
            direct_mime = ""
            direct_url = source.url
            source_probe = getattr(self.fetcher, "probe", None)
            if (
                direct_type is None
                and allowed_types == (ContentType.VIDEO,)
                and callable(source_probe)
            ):
                probe = await source_probe(
                    source.url, headers=AntiBotFetcher.source_headers(source)
                )
                if probe and 200 <= probe.status < 300:
                    direct_type = self.extractor._mime_type(probe.content_type)
                    direct_mime = probe.content_type
                    direct_url = probe.url or source.url
            if direct_type in allowed_types:
                asset = await self._try_candidates(
                    source,
                    FetchResponse(source.url, 200, "", b""),
                    [
                        Candidate(
                            direct_type,
                            url=direct_url,
                            mime_type=direct_mime,
                            selector="__source_probe__" if direct_mime else "__source_direct__",
                            referer=source.url if direct_url != source.url else "",
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
        keys = asset.history_keys or (asset.asset_key,)
        base_keys = tuple(
            dict.fromkeys(asset_key.split(":pdf", 1)[0].split(":zip", 1)[0] for asset_key in keys)
        )
        try:
            for base_key in base_keys:
                await self.cache.mark_sent(source.key, base_key)
        finally:
            async with self._pixiv_selection_lock:
                reserved = self._pixiv_reserved.get(source.key)
                if reserved is not None:
                    reserved.difference_update(base_keys)
                    if not reserved:
                        self._pixiv_reserved.pop(source.key, None)
        if cache_days == 0:
            await self.cache.cleanup({source.key: 0})

    async def _collect_pixiv_work(
        self,
        source: SourceConfig,
        candidates: list[Candidate],
        query: str,
    ) -> CollectedAsset | None:
        groups: dict[str, list[Candidate]] = {}
        for candidate in self.extractor._dedupe(candidates):
            key = candidate.group_key or candidate.url
            groups.setdefault(key, []).append(candidate)
        group_keys = list(groups)
        self._rng.shuffle(group_keys)
        source_response = FetchResponse(source.url, 200, "application/json", b"")
        download_errors: list[str] = []
        rejected_for_non_dedupe_reason = False

        async def materialize_page(
            page: Candidate, cached_asset: CollectedAsset | None
        ) -> CollectedAsset:
            if cached_asset is not None:
                return cached_asset
            return await self._materialize(source, page, source_response)

        for group_key in group_keys[:100]:
            pages = sorted(groups[group_key], key=lambda item: item.page_index)
            if not await self.cache.is_allowed(source.key, group_key, source.dedupe):
                continue
            cached = await asyncio.gather(
                *(self.cache.get_asset_by_origin(source.key, page.url) for page in pages)
            )
            cached_allowed = await asyncio.gather(
                *(
                    self.cache.is_allowed(source.key, asset.asset_key, source.dedupe)
                    for asset in cached
                    if asset is not None
                )
            )
            if not all(cached_allowed):
                continue
            results = await asyncio.gather(
                *(materialize_page(page, asset) for page, asset in zip(pages, cached, strict=True)),
                return_exceptions=True,
            )
            if any(isinstance(result, BaseException) for result in results):
                rejected_for_non_dedupe_reason = True
                download_errors.extend(
                    str(result) for result in results if isinstance(result, BaseException)
                )
                continue
            assets = [result for result in results if isinstance(result, CollectedAsset)]
            if len(assets) != len(pages):
                continue
            allowed = await asyncio.gather(
                *(
                    self.cache.is_allowed(source.key, asset.asset_key, source.dedupe)
                    for asset in assets
                )
            )
            if not all(allowed):
                continue
            matches = await asyncio.gather(
                *(
                    self._image_asset_matches(asset, page, query, file_validated=True)
                    for asset, page in zip(assets, pages, strict=True)
                )
            )
            if not all(matches):
                rejected_for_non_dedupe_reason = True
                continue
            asset_keys = tuple(asset.asset_key for asset in assets)
            selection_keys = (group_key, *asset_keys)
            if source.dedupe >= 0:
                async with self._pixiv_selection_lock:
                    reserved = self._pixiv_reserved.setdefault(source.key, set())
                    if reserved.intersection(selection_keys):
                        continue
                    still_allowed = await asyncio.gather(
                        *(
                            self.cache.is_allowed(source.key, asset_key, source.dedupe)
                            for asset_key in selection_keys
                        )
                    )
                    if not all(still_allowed):
                        continue
                    reserved.update(selection_keys)
            primary = assets[0]
            for page_asset, page in zip(assets, pages, strict=True):
                page_asset.r18 = page.r18
            primary.attachments = assets[1:]
            primary.history_keys = selection_keys
            primary.r18 = any(page.r18 for page in pages)
            return primary
        if download_errors:
            unique_errors = list(dict.fromkeys(download_errors))
            details = "；".join(unique_errors[-3:])
            raise PixivError(f"Pixiv 图片下载失败：{details}")
        if rejected_for_non_dedupe_reason:
            raise PixivError("Pixiv 图片下载完成但未通过文件或尺寸校验")
        return None

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
        detail_embed_links: dict[str, str] = {}
        embedded_candidates: list[Candidate] | None = None
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
                    detail_candidates, detail_embed_links = await self._crawl_detail_candidates(
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
                if embedded_candidates is None and detail_embed_links:
                    embedded_candidates = await self._crawl_embed_pages(
                        source, detail_embed_links, allowed_types
                    )
                if embedded_candidates:
                    asset = await self._try_candidates(
                        source,
                        source_response,
                        [item for item in embedded_candidates if item.content_type is content_type],
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

        raise CollectionError("解析到了页面，但没有符合类型及去重规则的可发送内容")

    async def _try_candidates(
        self,
        source: SourceConfig,
        source_response: FetchResponse,
        candidates: list[Candidate],
        query: str = "",
    ) -> CollectedAsset | None:
        pending = self.extractor._dedupe(list(candidates))
        if pending and all(item.content_type is ContentType.IMAGE for item in pending):
            pending.sort(key=lambda item: self._image_candidate_score(item, query, 0), reverse=True)
        else:
            self._rng.shuffle(pending)
        for offset in range(0, min(len(pending), 300), 100):
            pending_batch = pending[offset : offset + 100]
            # Random APIs and signed/media endpoints commonly reuse the same
            # URL for a different payload on every request.  Skip the URL
            # shortcut for those candidates so we can inspect the current
            # response and still reuse the local file when its content digest
            # is genuinely unchanged.  Stable URLs retain the fast cache path
            # regardless of the configured deduplication window.
            cacheable_urls = [
                candidate.url
                for candidate in pending_batch
                if candidate.url
                and candidate.selector
                not in {"__response__", "__source_probe__", "__source_direct__"}
                and not self._is_dynamic_candidate(candidate, source_response)
            ]
            cached_assets = await self.cache.get_allowed_assets_by_origins(
                source.key,
                cacheable_urls,
                source.dedupe,
            )
            batch = await self._prioritize_candidates(
                source, pending_batch, query, cached_assets=cached_assets
            )
            for candidate in batch:
                dynamic_origin = candidate.selector in {
                    "__response__",
                    "__source_probe__",
                    "__source_direct__",
                }
                if candidate.url and not dynamic_origin:
                    cached = cached_assets.get(candidate.url)
                    if cached and await self._image_asset_matches(cached, candidate, query):
                        return cached
                if (
                    candidate.content_type is ContentType.IMAGE
                    and self.image_min_bytes >= 0
                    and 0 < candidate.content_length < self.image_min_bytes
                ):
                    continue
                try:
                    asset = await self._materialize(source, candidate, source_response)
                except Exception:
                    continue
                if await self.cache.is_allowed(
                    source.key, asset.asset_key, source.dedupe
                ) and await self._image_asset_matches(
                    asset,
                    candidate,
                    query,
                    file_validated=(
                        candidate.content_type is ContentType.IMAGE
                        and candidate.selector != "__response__"
                    ),
                ):
                    return asset
        return None

    @staticmethod
    def _is_dynamic_candidate(candidate: Candidate, source_response: FetchResponse) -> bool:
        """Return whether a candidate URL is likely a rotating endpoint.

        A stable CDN URL is safe to reuse by origin.  URLs carrying random,
        nonce, expiry, or signature markers are commonly regenerated by the
        server, so resolving them again is necessary to avoid replaying the
        first response from the cache.
        """
        if candidate.selector in {"__response__", "__source_probe__", "__source_direct__"}:
            return True
        values = (candidate.url, source_response.url)
        markers = {
            "random",
            "rand",
            "shuffle",
            "nonce",
            "timestamp",
            "expires",
            "expiry",
            "signature",
            "sig",
            "r18",
        }
        for value in values:
            parts = urlsplit(value)
            path_tokens = set(filter(None, re.split(r"[/_.-]+", parts.path.lower())))
            query_tokens = {key.lower() for key, _ in parse_qsl(parts.query)}
            if markers.intersection(path_tokens | query_tokens):
                return True
        return False

    async def _prioritize_candidates(
        self,
        source: SourceConfig,
        candidates: list[Candidate],
        query: str = "",
        *,
        cached_assets: dict[str, CollectedAsset] | None = None,
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
            if candidate.selector in {"__response__", "__source_probe__", "__source_direct__"}:
                return (0, 1, 0, 0, index)
            if not candidate.url or candidate.content_type is ContentType.TEXT:
                return (1, 1, 0, 0, index)
            cached = (cached_assets or {}).get(candidate.url)
            if cached and cached.local_path:
                try:
                    size = cached.local_path.stat().st_size
                except OSError:
                    cached = None
            if cached:
                response = FetchResponse(
                    candidate.url,
                    200,
                    cached.mime_type,
                    b"",
                    headers={"content-length": str(size)},
                    transport="cache",
                )
            else:
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
                size = self._response_size(response)
            except (TypeError, ValueError):
                size = 0
            candidate.content_length = 0 if response.transport == "cache" else max(0, size)
            actual_type = self.extractor._mime_type(response.content_type)
            if candidate.content_type is ContentType.IMAGE:
                score = self._image_candidate_score(candidate, query, size)
                if 200 <= response.status < 300 and actual_type is ContentType.IMAGE:
                    return (0, 0, -score, -size, index)
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

    @staticmethod
    def _response_size(response: FetchResponse) -> int:
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in {"", "identity"}:
            return 0
        content_range = response.headers.get("content-range", "")
        range_total = re.search(r"/(\d+)\s*$", content_range)
        if range_total:
            return int(range_total.group(1))
        if response.status == 206:
            return 0
        return int(response.headers.get("content-length", "0") or 0)

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
        self,
        asset: CollectedAsset,
        candidate: Candidate,
        query: str,
        *,
        file_validated: bool = False,
    ) -> bool:
        if candidate.content_type is not ContentType.IMAGE:
            return True
        if file_validated:
            width, height = candidate.width, candidate.height
        else:
            if not asset.local_path or not asset.local_path.exists():
                return False
            try:
                if (
                    self.image_min_bytes >= 0
                    and asset.local_path.stat().st_size < self.image_min_bytes
                ):
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
        if candidate.referer and (
            same_origin or include_cross_origin_referer or source.template == "pixiv"
        ):
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
                downloaded = await self.fetcher.download(candidate.url, temporary, headers=headers)
                try:
                    mime_type = await validate_download(downloaded)
                except FetchError:
                    should_retry_with_referer = (
                        "Referer" not in headers
                        and bool(candidate.referer)
                        and urlsplit(candidate.url).hostname != urlsplit(source.url).hostname
                    )
                    if not should_retry_with_referer:
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
            AntiBotFetcher._validate_url(candidate.url)
        except FetchError as exc:
            raise CollectionError(str(exc)) from exc
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
        assets = await self.cache.list_allowed_assets(source.key, source.dedupe)
        for content_type in CONTENT_PRIORITY:
            if content_type not in allowed_types:
                continue
            for asset in assets:
                if asset.content_type is content_type and await self._image_asset_matches(
                    asset, Candidate(content_type=content_type), query
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
        candidates, embed_links = await self._crawl_detail_candidates(source, response, requested)
        if embed_links:
            candidates.extend(await self._crawl_embed_pages(source, embed_links, requested))
        return self.extractor._dedupe(candidates)

    async def _crawl_detail_candidates(
        self,
        source: SourceConfig,
        response: FetchResponse,
        requested: tuple[ContentType, ...],
    ) -> tuple[list[Candidate], dict[str, str]]:
        links = self.extractor.extract_links(response)
        if not links:
            return [], {}
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
        return self.extractor._dedupe(candidates), embed_links

    async def _crawl_embed_pages(
        self,
        source: SourceConfig,
        embed_links: dict[str, str],
        requested: tuple[ContentType, ...],
    ) -> list[Candidate]:
        if not embed_links:
            return []

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
        candidates: list[Candidate] = []
        for response_item in embedded:
            if isinstance(response_item, BaseException) or response_item.status >= 400:
                continue
            extracted, _ = self.extractor.extract(response_item, requested)
            candidates.extend(extracted)
        return self.extractor._dedupe(candidates)
