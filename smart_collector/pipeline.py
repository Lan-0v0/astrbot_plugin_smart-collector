from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .cache import CacheStore
from .extractor import AdaptiveExtractor
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


class CollectorPipeline:
    def __init__(self, data_dir: Path, *, concurrency: int = 4, timeout: float = 30.0) -> None:
        self.data_dir = data_dir
        self.cache = CacheStore(data_dir)
        self.fetcher = AntiBotFetcher(timeout=timeout, concurrency=concurrency)
        self.extractor = AdaptiveExtractor()
        self.postprocessor = PostProcessor(data_dir / "output")
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
    ) -> tuple[SourceConfig, CollectedAsset]:
        enabled = [source for source in sources if source.enabled]
        if not enabled:
            raise CollectionError("没有已启用的自定义爬取项")
        results = await asyncio.gather(
            *(self.collect(source, requested, user_key) for source in enabled),
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
    ) -> CollectedAsset:
        await self._check_rate(source, user_key)
        allowed_types = tuple(
            item for item in CONTENT_PRIORITY if item in (requested or source.content_types)
        )
        allowed_types = tuple(item for item in allowed_types if item in source.content_types)
        if not allowed_types:
            raise CollectionError(f"{source.name} 未配置所请求的内容类型")

        try:
            response = await self.fetcher.fetch_source(source)
            if response.status >= 400:
                raise FetchError(f"HTTP {response.status}")
            profile = await self.cache.get_profile(source.key)
            candidates, new_profile = self.extractor.extract(response, allowed_types, profile)
            if new_profile and new_profile != profile:
                await self.cache.save_profile(source.key, new_profile)
            if ContentType.VIDEO in allowed_types and not any(
                item.content_type is ContentType.VIDEO for item in candidates
            ):
                candidates.extend(await self._crawl_detail_pages(source, response, allowed_types))
            if ContentType.VIDEO in allowed_types and not any(
                item.content_type is ContentType.VIDEO for item in candidates
            ):
                candidates.extend(await self._yt_dlp_candidates(source.url))
            asset = await self._choose_candidate(source, candidates, allowed_types, response)
        except Exception as exc:
            cached = await self._cached_fallback(source, allowed_types)
            if cached:
                asset = cached
            else:
                raise CollectionError(f"{source.name} 抓取失败: {exc}") from exc

        if source.image_to_pdf and asset.content_type is ContentType.IMAGE:
            asset = await self.postprocessor.image_to_pdf(asset)
        if source.compress and asset.content_type in {ContentType.IMAGE, ContentType.VIDEO}:
            asset = await self.postprocessor.compress(asset, source.compression_password)
        return asset

    async def mark_sent(self, source: SourceConfig, asset: CollectedAsset) -> None:
        base_key = asset.asset_key.split(":pdf", 1)[0].split(":zip", 1)[0]
        await self.cache.mark_sent(source.key, base_key)
        if source.cache_days == 0:
            await self.cache.cleanup({source.key: 0})

    async def cleanup(self, sources: list[SourceConfig]) -> int:
        return await self.cache.cleanup({source.key: source.cache_days for source in sources})

    async def _choose_candidate(
        self,
        source: SourceConfig,
        candidates: list[Candidate],
        allowed_types: tuple[ContentType, ...],
        source_response: FetchResponse,
    ) -> CollectedAsset:
        ordered: list[Candidate] = []
        for content_type in CONTENT_PRIORITY:
            if content_type not in allowed_types:
                continue
            same_type = [item for item in candidates if item.content_type is content_type]
            ordered.extend(same_type)
        for candidate in ordered[:100]:
            direct_dynamic = candidate.selector == "__response__"
            if candidate.url and not direct_dynamic:
                cached = await self.cache.get_asset_by_origin(source.key, candidate.url)
                if cached and await self.cache.is_allowed(
                    source.key, cached.asset_key, source.dedupe
                ):
                    return cached
            try:
                asset = await self._materialize(source, candidate, source_response)
            except Exception:
                continue
            if await self.cache.is_allowed(source.key, asset.asset_key, source.dedupe):
                return asset
        cached = await self._cached_fallback(source, allowed_types)
        if cached:
            return cached
        raise CollectionError("解析到了页面，但没有符合类型及去重规则的可发送内容")

    async def _materialize(
        self,
        source: SourceConfig,
        candidate: Candidate,
        source_response: FetchResponse,
    ) -> CollectedAsset:
        if candidate.content_type is ContentType.TEXT:
            body = candidate.text.encode("utf-8")
            mime_type = "text/plain"
            origin_url = source_response.url
        else:
            response = (
                source_response
                if candidate.selector == "__response__" and candidate.url == source_response.url
                else await self.fetcher.fetch(
                    candidate.url,
                    headers=(
                        AntiBotFetcher.source_headers(source)
                        if urlsplit(candidate.url).hostname == urlsplit(source.url).hostname
                        else None
                    ),
                )
            )
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

        content_digest = hashlib.sha256(body).hexdigest()
        digest = hashlib.sha256(f"{source.key}:{content_digest}".encode()).hexdigest()
        cached = await self.cache.get_asset(digest)
        if cached:
            return cached
        extension = self._extension(origin_url, mime_type, candidate.content_type)
        target = self.cache.files_dir / f"{content_digest}{extension}"
        await asyncio.to_thread(target.write_bytes, body)
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

    async def _cached_fallback(
        self, source: SourceConfig, allowed_types: tuple[ContentType, ...]
    ) -> CollectedAsset | None:
        assets = await self.cache.list_assets(source.key)
        for content_type in CONTENT_PRIORITY:
            if content_type not in allowed_types:
                continue
            for asset in assets:
                if asset.content_type is content_type and await self.cache.is_allowed(
                    source.key, asset.asset_key, source.dedupe
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
    async def _yt_dlp_candidates(url: str) -> list[Candidate]:
        try:
            import yt_dlp
        except ImportError:
            return []

        def extract() -> list[Candidate]:
            options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": False,
            }
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
                        )
                    )
            return result

        try:
            return await asyncio.to_thread(extract)
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
        responses = await asyncio.gather(
            *(
                self.fetcher.fetch(url, headers=AntiBotFetcher.source_headers(source))
                for url in links
            ),
            return_exceptions=True,
        )
        candidates: list[Candidate] = []
        for detail in responses:
            if isinstance(detail, BaseException) or detail.status >= 400:
                continue
            extracted, _ = self.extractor.extract(detail, requested)
            candidates.extend(extracted)
        return candidates
