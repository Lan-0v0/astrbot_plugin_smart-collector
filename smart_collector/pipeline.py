from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import random
import re
import time
import uuid
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
    def __init__(
        self,
        data_dir: Path,
        *,
        concurrency: int = -1,
        timeout: float = -1,
        rng: random.Random | None = None,
    ) -> None:
        self.data_dir = data_dir
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
            asset = await self._collect_by_priority(source, response, candidates, allowed_types)
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
    ) -> CollectedAsset:
        detail_candidates: list[Candidate] | None = None
        for content_type in CONTENT_PRIORITY:
            if content_type not in allowed_types:
                continue
            asset = await self._try_candidates(
                source,
                source_response,
                [item for item in candidates if item.content_type is content_type],
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
                )
                if asset:
                    return asset
            if content_type is ContentType.VIDEO:
                asset = await self._try_candidates(
                    source,
                    source_response,
                    await self._yt_dlp_candidates(source_response.url, source.video_quality),
                )
                if asset:
                    return asset

        cached = await self._cached_fallback(source, allowed_types)
        if cached:
            return cached
        raise CollectionError("解析到了页面，但没有符合类型及去重规则的可发送内容")

    async def _try_candidates(
        self,
        source: SourceConfig,
        source_response: FetchResponse,
        candidates: list[Candidate],
    ) -> CollectedAsset | None:
        candidates = list(candidates)
        self._rng.shuffle(candidates)
        candidates = await self._prioritize_candidates(source, candidates)
        for candidate in candidates[:100]:
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
        return None

    async def _prioritize_candidates(
        self, source: SourceConfig, candidates: list[Candidate]
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
            if candidate.selector == "__response__":
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
            quality = quality_order(candidate, size)
            if 200 <= response.status < 300 and actual_type is candidate.content_type:
                return (0, *quality, index)
            if 200 <= response.status < 300:
                return (1, *quality, index)
            if response.status in {401, 403, 405, 429}:
                return (2, *quality, index)
            return (3, *quality, index)

        inspected = await asyncio.gather(
            *(inspect(index, candidate) for index, candidate in enumerate(candidates[:100]))
        )
        ranked = sorted(zip(inspected, candidates[:100], strict=True), key=lambda item: item[0])
        return [candidate for _, candidate in ranked] + candidates[100:]

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
            and urlsplit(candidate.url).path.lower().endswith(".m3u8")
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
            content_digest = hashlib.sha256(body).hexdigest()
            target = self.cache.files_dir / (
                content_digest + self._extension(origin_url, mime_type, candidate.content_type)
            )
            if not target.exists():
                await asyncio.to_thread(target.write_bytes, body)
        else:
            headers = self._candidate_headers(source, candidate)
            temporary = self.cache.files_dir / f".download-{uuid.uuid4().hex}"
            try:
                try:
                    downloaded = await self.fetcher.download(
                        candidate.url, temporary, headers=headers
                    )
                    if downloaded.status >= 400:
                        raise FetchError(f"资源返回 HTTP {downloaded.status}")
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
                if downloaded.status >= 400:
                    raise FetchError(f"资源返回 HTTP {downloaded.status}")
                mime_type = downloaded.content_type or candidate.mime_type
                actual_type = self.extractor._mime_type(mime_type)
                if actual_type and actual_type is not candidate.content_type:
                    raise CollectionError("资源响应类型与解析类型不一致")
                if "html" in mime_type or "json" in mime_type:
                    raise CollectionError("候选资源不是可下载媒体")
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
        headers = self._candidate_headers(source, candidate, include_cross_origin_referer=True)

        def download() -> Path:
            preferred_format = "best" if source.video_quality == "highest" else "worst"

            def run(format_selector: str | None) -> Path:
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

            try:
                return run(preferred_format)
            except Exception:
                for partial in self.cache.files_dir.glob(partial_stem.name + ".*"):
                    partial.unlink(missing_ok=True)
                return run(None)

        downloaded = await asyncio.to_thread(download)
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
    async def _yt_dlp_candidates(url: str, video_quality: str) -> list[Candidate]:
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
                            width=int(entry.get("width") or 0),
                            height=int(entry.get("height") or 0),
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

        embedded = await asyncio.gather(
            *(
                self.fetcher.fetch(
                    url,
                    headers={
                        **AntiBotFetcher.source_headers(source),
                        "Referer": referer,
                    },
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
