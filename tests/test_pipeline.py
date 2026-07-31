import asyncio
import hashlib
import io
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

from smart_collector.models import (
    Candidate,
    CollectedAsset,
    ContentType,
    DownloadedFile,
    FetchResponse,
    SourceConfig,
)
from smart_collector.pipeline import CollectorPipeline


class FakeFetcher:
    def __init__(self) -> None:
        self.media_fetches = 0

    async def fetch_source(self, source: SourceConfig) -> FetchResponse:
        return FetchResponse(
            url=source.url,
            status=200,
            content_type="text/html",
            body=b"<html><body><img src='/asset.png'></body></html>",
        )

    async def fetch(self, url: str, *, headers=None) -> FetchResponse:
        return FetchResponse(
            url=url, status=200, content_type="image/png", body=self._image_bytes()
        )

    async def download(self, url: str, destination: Path, *, headers=None) -> DownloadedFile:
        self.media_fetches += 1
        body = self._image_bytes()
        destination.write_bytes(body)
        return DownloadedFile(
            url=url,
            status=200,
            content_type="image/png",
            headers={},
            local_path=destination,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
        )

    async def close(self) -> None:
        return None

    @staticmethod
    def _image_bytes() -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (64, 64), "red").save(output, "PNG")
        return output.getvalue()


class FakeVideoFetcher:
    async def fetch_source(self, source: SourceConfig) -> FetchResponse:
        return FetchResponse(
            url=source.url,
            status=200,
            content_type="text/html",
            body=b"<html><a href='/archives/7'>video</a></html>",
        )

    async def fetch(self, url: str, *, headers=None) -> FetchResponse:
        if url == "https://avbebe.com/archives/7":
            return FetchResponse(
                url=url,
                status=200,
                content_type="text/html",
                body=b"<iframe src='https://player.example/e/7'></iframe>",
            )
        if url == "https://player.example/e/7":
            return FetchResponse(
                url=url,
                status=200,
                content_type="text/html",
                body=b"<script>var source='https://cdn.example/7.mp4';</script>",
            )
        return FetchResponse(url=url, status=200, content_type="video/mp4", body=b"video-data")

    async def close(self) -> None:
        return None

    async def download(self, url: str, destination: Path, *, headers=None) -> DownloadedFile:
        body = b"video-data"
        destination.write_bytes(body)
        return DownloadedFile(
            url=url,
            status=200,
            content_type="video/mp4",
            headers={},
            local_path=destination,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
        )


class FixedRandom:
    @staticmethod
    def randint(start: int, end: int) -> int:
        assert (start, end) == (1, 7262)
        return 4321

    @staticmethod
    def shuffle(values: list) -> None:
        return None


class FakePaginatedFetcher:
    def __init__(self) -> None:
        self.fetched: list[str] = []

    async def fetch_source(self, source: SourceConfig) -> FetchResponse:
        return FetchResponse(
            url=source.url,
            status=200,
            content_type="text/html",
            body=(
                b"<a href='/all?sort=favorite&amp;page=2'>2</a>"
                b"<a href='/all?sort=favorite&amp;page=7262'>last</a>"
            ),
        )

    async def fetch(self, url: str, *, headers=None) -> FetchResponse:
        self.fetched.append(url)
        if "page=4321" in url:
            return FetchResponse(
                url=url,
                status=200,
                content_type="text/html",
                body=b"<script>const video='https://cdn.example/random.mp4';</script>",
            )
        return FetchResponse(url=url, status=404, content_type="text/html", body=b"")

    async def download(self, url: str, destination: Path, *, headers=None) -> DownloadedFile:
        body = b"random-video"
        destination.write_bytes(body)
        return DownloadedFile(
            url=url,
            status=200,
            content_type="video/mp4",
            headers={},
            local_path=destination,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
        )

    async def close(self) -> None:
        return None


class FakeFallbackFetcher:
    def __init__(self) -> None:
        self.downloads: list[str] = []
        self.download_headers: list[dict[str, str]] = []

    async def fetch_source(self, source: SourceConfig) -> FetchResponse:
        return FetchResponse(
            url=source.url,
            status=200,
            content_type="text/html",
            body=(
                b"<script>const video='https://cdn.example/bad.mp4';</script>"
                b"<a href='/movie/7'>detail</a>"
            ),
        )

    async def fetch(self, url: str, *, headers=None) -> FetchResponse:
        if url == "https://example.com/movie/7":
            return FetchResponse(
                url=url,
                status=200,
                content_type="text/html",
                body=b"<video src='https://cdn.example/good.mp4'></video>",
            )
        return FetchResponse(url=url, status=404, content_type="text/html", body=b"")

    async def download(self, url: str, destination: Path, *, headers=None) -> DownloadedFile:
        self.downloads.append(url)
        self.download_headers.append(dict(headers or {}))
        if url.endswith("bad.mp4"):
            destination.write_bytes(b"")
            return DownloadedFile(url, 404, "", {}, destination, "", 0)
        body = b"fallback-video"
        destination.write_bytes(body)
        return DownloadedFile(
            url=url,
            status=200,
            content_type="video/mp4",
            headers={},
            local_path=destination,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
        )

    async def close(self) -> None:
        return None


def test_pipeline_reuses_cached_media(tmp_path: Path) -> None:
    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path, image_ignore_size_kb=-1)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = FakeFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:test",
            template="website",
            name="Test",
            enabled=True,
            url="https://example.com/page",
            content_types=(ContentType.IMAGE,),
            command="/test",
            dedupe=-1,
            rate_limit=-1,
        )
        first = await pipeline.collect(source, None, "user")
        second = await pipeline.collect(source, None, "user")
        assert first.exists
        assert not first.cached
        assert second.cached
        assert fake.media_fetches == 1
        await pipeline.close()

    asyncio.run(scenario())


def test_cached_candidate_skips_probe_and_download(tmp_path: Path) -> None:
    class NoNetworkFetcher:
        def __init__(self) -> None:
            self.probes = 0
            self.downloads = 0

        async def probe(self, url, *, headers=None):
            self.probes += 1
            raise AssertionError("cached media must not be probed")

        async def download(self, url, destination, *, headers=None):
            self.downloads += 1
            raise AssertionError("cached media must not be downloaded")

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = NoNetworkFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:cache-fast-path",
            template="website",
            name="Cache fast path",
            enabled=True,
            url="https://source.example/page",
            content_types=(ContentType.VIDEO,),
            command="/video",
            dedupe=-1,
        )
        media_path = pipeline.cache.files_dir / "cached.mp4"
        media_path.write_bytes(b"cached-video")
        cached = CollectedAsset(
            asset_key="cached-video",
            source_key=source.key,
            source_name=source.name,
            content_type=ContentType.VIDEO,
            origin_url="https://cdn.example/video.mp4",
            mime_type="video/mp4",
            local_path=media_path,
        )
        await pipeline.cache.save_asset(cached)
        asset = await pipeline._try_candidates(
            source,
            FetchResponse(source.url, 200, "text/html", b""),
            [Candidate(ContentType.VIDEO, url=cached.origin_url)],
        )
        assert asset and asset.cached
        assert (fake.probes, fake.downloads) == (0, 0)
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_known_small_image_is_skipped_before_download(tmp_path: Path) -> None:
    class SmallProbeFetcher:
        def __init__(self) -> None:
            self.downloads = 0

        async def probe(self, url, *, headers=None):
            return FetchResponse(
                url,
                200,
                "image/jpeg",
                b"",
                headers={"content-length": str(50 * 1024)},
            )

        async def download(self, url, destination, *, headers=None):
            self.downloads += 1
            raise AssertionError("known undersized images must not be downloaded")

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = SmallProbeFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:small-image",
            template="website",
            name="Small image",
            enabled=True,
            url="https://source.example/page",
            content_types=(ContentType.IMAGE,),
            command="/image",
        )
        candidate = Candidate(ContentType.IMAGE, url="https://cdn.example/small.jpg")
        asset = await pipeline._try_candidates(
            source,
            FetchResponse(source.url, 200, "text/html", b""),
            [candidate],
        )
        assert asset is None
        assert candidate.content_length == 50 * 1024
        assert fake.downloads == 0
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_duplicate_candidates_are_downloaded_only_once(tmp_path: Path) -> None:
    class FailedFetcher:
        def __init__(self) -> None:
            self.downloads = 0

        async def download(self, url, destination, *, headers=None):
            self.downloads += 1
            destination.write_bytes(b"missing")
            return DownloadedFile(url, 404, "text/html", {}, destination, "", 7)

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = FailedFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:dedupe-candidates",
            template="website",
            name="Dedupe candidates",
            enabled=True,
            url="https://source.example/page",
            content_types=(ContentType.AUDIO,),
            command="/audio",
        )
        url = "https://cdn.example/audio.mp3"
        asset = await pipeline._try_candidates(
            source,
            FetchResponse(source.url, 200, "text/html", b""),
            [
                Candidate(ContentType.AUDIO, url=url, source_kind="script"),
                Candidate(ContentType.AUDIO, url=url, source_kind="html"),
            ],
        )
        assert asset is None
        assert fake.downloads == 1
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_disabled_dedupe_refreshes_reused_video_url(tmp_path: Path) -> None:
    class RotatingFetcher:
        def __init__(self) -> None:
            self.downloads = 0

        async def download(self, url, destination, *, headers=None):
            self.downloads += 1
            body = f"video-{self.downloads}".encode()
            destination.write_bytes(body)
            return DownloadedFile(
                url,
                200,
                "video/mp4",
                {},
                destination,
                hashlib.sha256(body).hexdigest(),
                len(body),
            )

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = RotatingFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:rotating-video",
            template="website",
            name="Rotating video",
            enabled=True,
            url="https://source.example/random",
            content_types=(ContentType.VIDEO,),
            command="/rotating",
            dedupe=-1,
            rate_limit=-1,
        )
        candidate = Candidate(ContentType.VIDEO, url="https://cdn.example/random.mp4")
        response = FetchResponse(source.url, 200, "text/html", b"")
        first = await pipeline._try_candidates(source, response, [candidate])
        second = await pipeline._try_candidates(source, response, [candidate])
        assert first and second
        assert first.asset_key != second.asset_key
        assert fake.downloads == 2
        await pipeline.close()

    asyncio.run(scenario())


def test_pipeline_fetches_a_random_discovered_page(tmp_path: Path) -> None:
    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path, rng=FixedRandom())  # type: ignore[arg-type]
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = FakePaginatedFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:paged",
            template="website",
            name="Paged",
            enabled=True,
            url="https://example.com/all",
            content_types=(ContentType.VIDEO,),
            command="/paged",
            dedupe=-1,
            rate_limit=-1,
        )
        asset = await pipeline.collect(source, None, "user")
        assert fake.fetched[0] == "https://example.com/all?sort=favorite&page=4321"
        assert asset.origin_url == "https://cdn.example/random.mp4"
        await pipeline.close()

    asyncio.run(scenario())


def test_direct_media_url_uses_streaming_download_first(tmp_path: Path) -> None:
    class DirectFetcher:
        def __init__(self) -> None:
            self.fetch_source_called = False

        async def probe(self, url, *, headers=None):
            return FetchResponse(url, 200, "video/mp4", b"")

        async def fetch_source(self, source):
            self.fetch_source_called = True
            raise AssertionError("direct media must not use the buffered page fetch")

        async def download(self, url, destination, *, headers=None):
            body = b"large-streamed-video"
            destination.write_bytes(body)
            return DownloadedFile(
                url,
                200,
                "video/mp4",
                {},
                destination,
                hashlib.sha256(body).hexdigest(),
                len(body),
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = DirectFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:direct",
            template="website",
            name="Direct",
            enabled=True,
            url="https://cdn.example/movie.mp4",
            content_types=(ContentType.VIDEO,),
            command="/direct",
            dedupe=-1,
            rate_limit=-1,
        )
        asset = await pipeline.collect(source, None, "user")
        assert asset.exists
        assert asset.origin_url == source.url
        assert not fake.fetch_source_called
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_extensionless_video_uses_probe_then_streaming_download(tmp_path: Path) -> None:
    class ExtensionlessFetcher:
        def __init__(self) -> None:
            self.probes = 0
            self.fetch_source_called = False

        async def probe(self, url, *, headers=None):
            self.probes += 1
            return FetchResponse(url, 200, "video/mp4", b"")

        async def fetch_source(self, source):
            self.fetch_source_called = True
            raise AssertionError("extensionless media must not enter the page buffer")

        async def download(self, url, destination, *, headers=None):
            body = b"extensionless-stream"
            destination.write_bytes(body)
            return DownloadedFile(
                url,
                200,
                "video/mp4",
                {},
                destination,
                hashlib.sha256(body).hexdigest(),
                len(body),
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = ExtensionlessFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:extensionless",
            template="website",
            name="Extensionless",
            enabled=True,
            url="https://cdn.example/play?id=1",
            content_types=(ContentType.VIDEO,),
            command="/extensionless",
            dedupe=-1,
            rate_limit=-1,
        )
        asset = await pipeline.collect(source, None, "user")
        assert asset.exists and asset.mime_type == "video/mp4"
        assert fake.probes == 1
        assert not fake.fetch_source_called
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_dynamic_direct_media_endpoint_does_not_reuse_origin_cache(tmp_path: Path) -> None:
    class RandomImageFetcher:
        def __init__(self) -> None:
            self.probes = 0
            self.downloads = 0
            self.fetch_source_called = False

        async def probe(self, url, *, headers=None):
            self.probes += 1
            raise AssertionError("known media extensions must not be probed")

        async def fetch_source(self, source):
            self.fetch_source_called = True
            raise AssertionError("dynamic direct images must use streaming download")

        async def download(self, url, destination, *, headers=None):
            self.downloads += 1
            output = io.BytesIO()
            color = "red" if self.downloads == 1 else "blue"
            Image.new("RGB", (64, 64), color).save(output, "PNG")
            body = output.getvalue()
            destination.write_bytes(body)
            return DownloadedFile(
                url,
                200,
                "image/png",
                {},
                destination,
                hashlib.sha256(body).hexdigest(),
                len(body),
            )

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path, image_ignore_size_kb=-1)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = RandomImageFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:random-image",
            template="website",
            name="Random image",
            enabled=True,
            url="https://images.example/random.png?r18=1",
            content_types=(ContentType.IMAGE,),
            command="/image",
            dedupe=-1,
            rate_limit=-1,
        )
        first = await pipeline.collect(source, None, "user")
        second = await pipeline.collect(source, None, "user")
        assert first.asset_key != second.asset_key
        assert first.local_path and second.local_path
        assert first.local_path.read_bytes() != second.local_path.read_bytes()
        assert (fake.probes, fake.downloads) == (0, 2)
        assert not fake.fetch_source_called
        await pipeline.close()

    asyncio.run(scenario())


def test_extensionless_direct_image_uses_one_fresh_get_per_collection(tmp_path: Path) -> None:
    class RandomImageFetcher:
        def __init__(self) -> None:
            self.probes = 0
            self.fetches = 0
            self.downloads = 0

        async def probe(self, url, *, headers=None):
            self.probes += 1
            raise AssertionError("image-only sources must not make a separate probe request")

        async def fetch_source(self, source):
            self.fetches += 1
            output = io.BytesIO()
            color = "red" if self.fetches == 1 else "blue"
            Image.new("RGB", (64, 64), color).save(output, "PNG")
            return FetchResponse(source.url, 200, "image/png", output.getvalue())

        async def download(self, url, destination, *, headers=None):
            self.downloads += 1
            raise AssertionError("the fetched image response must be materialized directly")

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path, image_ignore_size_kb=-1)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = RandomImageFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:extensionless-random-image",
            template="website",
            name="Extensionless random image",
            enabled=True,
            url="https://images.example/random?r18=1",
            content_types=(ContentType.IMAGE,),
            command="/image",
            dedupe=-1,
            rate_limit=-1,
        )
        first = await pipeline.collect(source, None, "user")
        second = await pipeline.collect(source, None, "user")
        assert first.asset_key != second.asset_key
        assert (fake.probes, fake.fetches, fake.downloads) == (0, 2, 0)
        await pipeline.close()

    asyncio.run(scenario())


def test_video_candidates_are_tried_in_later_batches(tmp_path: Path) -> None:
    class BatchFetcher:
        async def download(self, url, destination, *, headers=None):
            if url.endswith("good.mp4"):
                body = b"working-video"
                status = 200
                mime = "video/mp4"
            else:
                body = b"missing"
                status = 404
                mime = "text/html"
            destination.write_bytes(body)
            return DownloadedFile(
                url,
                status,
                mime,
                {},
                destination,
                hashlib.sha256(body).hexdigest(),
                len(body),
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path, rng=FixedRandom())  # type: ignore[arg-type]
        await pipeline.initialize()
        await pipeline.fetcher.close()
        pipeline.fetcher = BatchFetcher()  # type: ignore[assignment]
        source = SourceConfig(
            key="website:batches",
            template="website",
            name="Batches",
            enabled=True,
            url="https://source.example/page",
            content_types=(ContentType.VIDEO,),
            command="/batches",
            dedupe=-1,
        )
        candidates = [
            Candidate(ContentType.VIDEO, url=f"https://cdn.example/bad-{index}.mp4")
            for index in range(100)
        ]
        candidates.append(Candidate(ContentType.VIDEO, url="https://cdn.example/good.mp4"))
        asset = await pipeline._try_candidates(
            source,
            FetchResponse(source.url, 200, "text/html", b""),
            candidates,
        )
        assert asset and asset.origin_url.endswith("good.mp4")
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_cross_origin_embed_does_not_receive_source_secrets(tmp_path: Path) -> None:
    class EmbedFetcher:
        def __init__(self) -> None:
            self.requests: list[tuple[str, dict[str, str]]] = []

        async def fetch(self, url, *, headers=None):
            current = dict(headers or {})
            self.requests.append((url, current))
            if url == "https://source.example/video/1":
                return FetchResponse(
                    url,
                    200,
                    "text/html",
                    b"<iframe src='https://player.example/embed/1'></iframe>",
                )
            return FetchResponse(url, 200, "text/html", b"<html></html>")

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.fetcher.close()
        fake = EmbedFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:embed",
            template="website",
            name="Embed",
            enabled=True,
            url="https://source.example/list",
            content_types=(ContentType.VIDEO,),
            command="/embed",
            cookies=("session=secret",),
            headers={"Authorization": "secret"},
        )
        listing = FetchResponse(
            source.url,
            200,
            "text/html",
            b"<a href='/video/1'>video</a>",
        )
        await pipeline._crawl_detail_pages(source, listing, (ContentType.VIDEO,))
        assert fake.requests[0][1]["Authorization"] == "secret"
        assert fake.requests[1] == (
            "https://player.example/embed/1",
            {"Referer": "https://source.example/video/1"},
        )

    asyncio.run(scenario())


def test_pipeline_falls_back_to_detail_after_bad_page_candidate(tmp_path: Path) -> None:
    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path, rng=FixedRandom())  # type: ignore[arg-type]
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = FakeFallbackFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:fallback",
            template="website",
            name="Fallback",
            enabled=True,
            url="https://example.com/all",
            content_types=(ContentType.VIDEO,),
            command="/fallback",
            dedupe=-1,
            rate_limit=-1,
        )
        asset = await pipeline.collect(source, None, "user")
        assert fake.downloads == [
            "https://cdn.example/bad.mp4",
            "https://cdn.example/bad.mp4",
            "https://cdn.example/good.mp4",
        ]
        assert fake.download_headers[:2] == [
            {},
            {"Referer": "https://example.com/all"},
        ]
        assert asset.origin_url == "https://cdn.example/good.mp4"
        await pipeline.close()

    asyncio.run(scenario())


def test_cross_origin_media_does_not_leak_headers_or_referer_by_default() -> None:
    source = SourceConfig(
        key="website:headers",
        template="website",
        name="Headers",
        enabled=True,
        url="https://source.example/page",
        content_types=(ContentType.VIDEO,),
        command="/headers",
        cookies=("session=secret",),
        headers={"Authorization": "secret"},
    )
    candidate = Candidate(
        ContentType.VIDEO,
        url="https://cdn.example/video.mp4",
        referer=source.url,
    )
    assert CollectorPipeline._candidate_headers(source, candidate) == {}
    assert CollectorPipeline._candidate_headers(
        source, candidate, include_cross_origin_referer=True
    ) == {"Referer": source.url}


def test_cross_origin_probe_retries_with_referer_after_forbidden(tmp_path: Path) -> None:
    class ProbeFetcher:
        def __init__(self) -> None:
            self.headers: list[dict[str, str]] = []

        async def probe(self, url: str, *, headers=None) -> FetchResponse:
            current = dict(headers or {})
            self.headers.append(current)
            return FetchResponse(
                url=url,
                status=200 if "Referer" in current else 403,
                content_type="video/mp4" if "Referer" in current else "text/html",
                body=b"",
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.fetcher.close()
        fake = ProbeFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:probe",
            template="website",
            name="Probe",
            enabled=True,
            url="https://source.example/page",
            content_types=(ContentType.VIDEO,),
            command="/probe",
        )
        candidate = Candidate(
            ContentType.VIDEO,
            url="https://cdn.example/video.mp4",
            referer=source.url,
        )
        ranked = await pipeline._prioritize_candidates(source, [candidate])
        assert ranked == [candidate]
        assert fake.headers == [{}, {"Referer": source.url}]

    asyncio.run(scenario())


def test_cross_origin_download_retries_with_referer_after_unauthorized(
    tmp_path: Path,
) -> None:
    class RefererFetcher:
        def __init__(self) -> None:
            self.headers: list[dict[str, str]] = []

        async def download(self, url: str, destination: Path, *, headers=None) -> DownloadedFile:
            current = dict(headers or {})
            self.headers.append(current)
            if "Referer" not in current:
                destination.write_bytes(b"unauthorized")
                return DownloadedFile(url, 401, "text/html", {}, destination, "", 12)
            body = b"video-data"
            destination.write_bytes(body)
            return DownloadedFile(
                url=url,
                status=200,
                content_type="video/mp4",
                headers={},
                local_path=destination,
                sha256=hashlib.sha256(body).hexdigest(),
                size=len(body),
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = RefererFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:download",
            template="website",
            name="Download",
            enabled=True,
            url="https://source.example/page",
            content_types=(ContentType.VIDEO,),
            command="/download",
        )
        candidate = Candidate(
            ContentType.VIDEO,
            url="https://cdn.example/video.mp4",
            referer=source.url,
        )
        asset = await pipeline._materialize(
            source,
            candidate,
            FetchResponse(source.url, 200, "text/html", b""),
        )
        assert asset.local_path and asset.local_path.read_bytes() == b"video-data"
        assert fake.headers == [{}, {"Referer": source.url}]
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_cross_origin_image_retries_with_referer_after_placeholder(
    tmp_path: Path,
) -> None:
    class PlaceholderFetcher:
        def __init__(self) -> None:
            self.headers: list[dict[str, str]] = []

        async def download(self, url, destination, *, headers=None):
            current = dict(headers or {})
            self.headers.append(current)
            output = io.BytesIO()
            size = (128, 96) if "Referer" in current else (1, 1)
            Image.new("RGB", size, "blue").save(output, "PNG")
            body = output.getvalue()
            destination.write_bytes(body)
            return DownloadedFile(
                url,
                200,
                "image/png",
                {},
                destination,
                hashlib.sha256(body).hexdigest(),
                len(body),
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path, image_ignore_size_kb=-1)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = PlaceholderFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:image-referer",
            template="website",
            name="Image Referer",
            enabled=True,
            url="https://source.example/page",
            content_types=(ContentType.IMAGE,),
            command="/image",
        )
        candidate = Candidate(
            ContentType.IMAGE,
            url="https://cdn.example/image.png",
            referer=source.url,
        )
        asset = await pipeline._materialize(
            source, candidate, FetchResponse(source.url, 200, "text/html", b"")
        )
        assert asset.exists
        assert candidate.width == 128 and candidate.height == 96
        assert fake.headers == [{}, {"Referer": source.url}]
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_video_quality_orders_known_resolutions_and_unknown_sizes(tmp_path: Path) -> None:
    class QualityFetcher:
        async def probe(self, url: str, *, headers=None) -> FetchResponse:
            size = {
                "https://cdn.example/unknown-small.mp4": 100,
                "https://cdn.example/unknown-large.mp4": 1000,
            }.get(url, 500)
            return FetchResponse(
                url=url,
                status=200,
                content_type="video/mp4",
                body=b"",
                headers={"content-length": str(size)},
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.fetcher.close()
        pipeline.fetcher = QualityFetcher()  # type: ignore[assignment]
        source = SourceConfig(
            key="website:quality",
            template="website",
            name="Quality",
            enabled=True,
            url="https://source.example/page",
            content_types=(ContentType.VIDEO,),
            command="/quality",
        )
        low = Candidate(
            ContentType.VIDEO, url="https://cdn.example/640x360.mp4", width=640, height=360
        )
        high = Candidate(
            ContentType.VIDEO,
            url="https://cdn.example/1920x1080.mp4",
            width=1920,
            height=1080,
        )
        unknown_small = Candidate(ContentType.VIDEO, url="https://cdn.example/unknown-small.mp4")
        unknown_large = Candidate(ContentType.VIDEO, url="https://cdn.example/unknown-large.mp4")
        ranked = await pipeline._prioritize_candidates(
            source, [high, unknown_large, low, unknown_small]
        )
        assert ranked == [low, high, unknown_small, unknown_large]
        source.video_quality = "highest"
        ranked = await pipeline._prioritize_candidates(
            source, [low, unknown_small, high, unknown_large]
        )
        assert ranked == [high, low, unknown_large, unknown_small]

    asyncio.run(scenario())


def test_probe_size_uses_range_total_and_ignores_encoded_length() -> None:
    ranged = FetchResponse(
        "https://cdn.example/image.jpg",
        206,
        "image/jpeg",
        b"",
        headers={"content-length": "1", "content-range": "bytes 0-0/250000"},
    )
    assert CollectorPipeline._response_size(ranged) == 250_000
    encoded = FetchResponse(
        "https://cdn.example/image.jpg",
        200,
        "image/jpeg",
        b"",
        headers={"content-length": "50000", "content-encoding": "gzip"},
    )
    assert CollectorPipeline._response_size(encoded) == 0


def test_image_ranking_prefers_relevant_main_content_over_icons_and_ads(
    tmp_path: Path,
) -> None:
    class ImageProbeFetcher:
        async def probe(self, url: str, *, headers=None) -> FetchResponse:
            sizes = {
                "https://cdn.example/logo.png": 100_000,
                "https://cdn.example/banner.jpg": 2_000_000,
                "https://cdn.example/cat.jpg": 900_000,
            }
            return FetchResponse(
                url,
                200,
                "image/jpeg",
                b"",
                headers={"content-length": str(sizes[url])},
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path, image_ignore_size_kb=-1)
        await pipeline.fetcher.close()
        pipeline.fetcher = ImageProbeFetcher()  # type: ignore[assignment]
        source = SourceConfig(
            key="website:image-ranking",
            template="website",
            name="Images",
            enabled=True,
            url="https://source.example/gallery",
            content_types=(ContentType.IMAGE,),
            command="/images",
        )
        logo = Candidate(
            ContentType.IMAGE,
            url="https://cdn.example/logo.png",
            context_text="site logo icon",
            source_kind="image_element",
            width=512,
            height=512,
        )
        banner = Candidate(
            ContentType.IMAGE,
            url="https://cdn.example/banner.jpg",
            context_text="sponsor banner advertisement",
            source_kind="open_graph",
            width=2400,
            height=300,
        )
        cat = Candidate(
            ContentType.IMAGE,
            url="https://cdn.example/cat.jpg",
            context_text="蓝色猫咪夜景壁纸",
            source_kind="srcset",
            in_main_content=True,
            width=1600,
            height=1200,
        )
        ranked = await pipeline._prioritize_candidates(source, [logo, banner, cat], "图片 蓝色猫咪")
        assert ranked == [cat, logo, banner]

    asyncio.run(scenario())


def test_image_query_constraints_validate_downloaded_dimensions(tmp_path: Path) -> None:
    async def scenario() -> None:
        image_path = tmp_path / "landscape.png"
        Image.new("RGB", (800, 600), "blue").save(image_path)
        asset = CollectedAsset(
            asset_key="image",
            source_key="source",
            source_name="Source",
            content_type=ContentType.IMAGE,
            origin_url="https://example.com/image.png",
            mime_type="image/png",
            local_path=image_path,
        )
        candidate = Candidate(ContentType.IMAGE)
        pipeline = CollectorPipeline(tmp_path, image_ignore_size_kb=-1)
        assert await pipeline._image_asset_matches(asset, candidate, "图片 横屏")
        assert not await pipeline._image_asset_matches(asset, candidate, "图片 竖屏")
        assert not await pipeline._image_asset_matches(asset, candidate, "图片 至少1920x1080")
        await pipeline.fetcher.close()

    asyncio.run(scenario())


def test_image_ignore_size_uses_actual_file_size_and_supports_disable(tmp_path: Path) -> None:
    def image_bytes(size: int) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (800, 600), "blue").save(output, "PNG")
        return output.getvalue().ljust(size, b"\0")

    async def scenario() -> None:
        small_path = tmp_path / "small.png"
        small_path.write_bytes(image_bytes(100 * 1024 - 1))
        boundary_path = tmp_path / "boundary.png"
        boundary_path.write_bytes(image_bytes(100 * 1024))

        def asset(path: Path) -> CollectedAsset:
            return CollectedAsset(
                asset_key=path.stem,
                source_key="source",
                source_name="Source",
                content_type=ContentType.IMAGE,
                origin_url=f"https://example.com/{path.name}",
                mime_type="image/png",
                local_path=path,
            )

        default_pipeline = CollectorPipeline(tmp_path)
        assert not await default_pipeline._image_asset_matches(
            asset(small_path), Candidate(ContentType.IMAGE), ""
        )
        assert await default_pipeline._image_asset_matches(
            asset(boundary_path), Candidate(ContentType.IMAGE), ""
        )
        await default_pipeline.fetcher.close()

        disabled_pipeline = CollectorPipeline(tmp_path, image_ignore_size_kb=-1)
        assert await disabled_pipeline._image_asset_matches(
            asset(small_path), Candidate(ContentType.IMAGE), ""
        )
        await disabled_pipeline.fetcher.close()

    asyncio.run(scenario())


def test_image_ignore_size_rejects_small_download_before_cache(tmp_path: Path) -> None:
    output = io.BytesIO()
    Image.new("RGB", (800, 600), "blue").save(output, "PNG")
    image = output.getvalue()

    class SmallImageFetcher:
        async def download(self, url, destination, *, headers=None):
            destination.write_bytes(image)
            return DownloadedFile(
                url,
                200,
                "image/png",
                {},
                destination,
                hashlib.sha256(image).hexdigest(),
                500_000,
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        pipeline.fetcher = SmallImageFetcher()  # type: ignore[assignment]
        source = SourceConfig(
            key="website:size-limit",
            template="website",
            name="Size limit",
            enabled=True,
            url="https://example.com/gallery",
            content_types=(ContentType.IMAGE,),
            command="/image",
        )
        candidate = Candidate(ContentType.IMAGE, url="https://example.com/small.png")
        with pytest.raises(Exception, match="小于 100 KB"):
            await pipeline._materialize(
                source, candidate, FetchResponse(source.url, 200, "text/html", b"")
            )
        assert await pipeline.cache.list_assets(source.key) == []
        assert list(pipeline.cache.files_dir.glob(".download-*")) == []
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_image_query_parser_separates_positive_negative_and_hard_requirements() -> None:
    positive, negative, orientation, minimum = CollectorPipeline._image_query_requirements(
        "图片 蓝色夜景 横屏 1920x1080 不要人物"
    )
    assert "蓝色夜景" in positive
    assert "人物" in negative
    assert orientation == "landscape"
    assert minimum == (1920, 1080)


def test_pipeline_uses_image_description_to_select_relevant_content(tmp_path: Path) -> None:
    class AccurateImageFetcher:
        def __init__(self) -> None:
            self.downloads: list[str] = []

        async def fetch_source(self, source):
            return FetchResponse(
                source.url,
                200,
                "text/html",
                b"""<html><head><title>Gallery</title>
                <meta property='og:image' content='https://cdn.example/ad-banner.jpg'>
                </head><body><header><img src='https://cdn.example/logo.png'
                alt='site logo' width='256' height='256'></header>
                <main><figure><img src='https://cdn.example/cat.jpg'
                alt='blue cat wallpaper' width='1280' height='960'>
                <figcaption>Blue cat at night</figcaption></figure></main></body></html>""",
            )

        async def probe(self, url, *, headers=None):
            if url == "https://source.example/gallery":
                return FetchResponse(url, 200, "text/html", b"")
            sizes = {
                "https://cdn.example/ad-banner.jpg": 2_000_000,
                "https://cdn.example/logo.png": 200_000,
                "https://cdn.example/cat.jpg": 900_000,
            }
            return FetchResponse(
                url,
                200,
                "image/jpeg" if url.endswith(".jpg") else "image/png",
                b"",
                headers={"content-length": str(sizes[url])},
            )

        async def download(self, url, destination, *, headers=None):
            self.downloads.append(url)
            size = (1280, 960) if url.endswith("cat.jpg") else (2400, 300)
            output = io.BytesIO()
            Image.new("RGB", size, "blue").save(output, "JPEG")
            body = output.getvalue()
            destination.write_bytes(body)
            return DownloadedFile(
                url,
                200,
                "image/jpeg",
                {},
                destination,
                hashlib.sha256(body).hexdigest(),
                len(body),
            )

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path, image_ignore_size_kb=-1)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = AccurateImageFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:accurate-image",
            template="website",
            name="Accurate image",
            enabled=True,
            url="https://source.example/gallery",
            content_types=(ContentType.IMAGE,),
            command="/image",
            dedupe=-1,
            rate_limit=-1,
        )
        asset = await pipeline.collect(source, None, "user", "图片 blue cat wallpaper")
        assert asset.origin_url == "https://cdn.example/cat.jpg"
        assert fake.downloads == ["https://cdn.example/cat.jpg"]
        await pipeline.cache.close()

    asyncio.run(scenario())


def test_yt_dlp_quality_falls_back_to_automatic_format(monkeypatch) -> None:
    requested_formats: list[str | None] = []

    class YoutubeDL:
        def __init__(self, options):
            self.options = options
            requested_formats.append(options.get("format"))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, download=False):
            if self.options.get("format"):
                raise RuntimeError("preferred format unavailable")
            return {
                "url": "https://cdn.example/auto.mp4",
                "title": "Auto",
                "width": 1280,
                "height": 720,
            }

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=YoutubeDL))
    candidates = asyncio.run(
        CollectorPipeline._yt_dlp_candidates(
            "https://example.com/watch/1", "highest", {"Cookie": "session=1"}
        )
    )
    assert requested_formats == ["best", None]
    assert len(candidates) == 2
    assert candidates[0].url == "https://cdn.example/auto.mp4"
    assert candidates[0].referer == "https://example.com/watch/1"
    assert (candidates[0].width, candidates[0].height) == (1280, 720)
    assert candidates[1].selector == "yt-dlp-download"
    assert candidates[1].url == "https://example.com/watch/1"


def test_hls_retries_without_then_with_cross_origin_referer_and_cleans_up(
    monkeypatch, tmp_path: Path
) -> None:
    attempts: list[tuple[str | None, dict[str, str]]] = []

    class YoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, download=True):
            attempts.append(
                (self.options.get("format"), dict(self.options.get("http_headers") or {}))
            )
            Path(self.options["outtmpl"].replace("%(ext)s", "part")).write_bytes(b"partial")
            raise RuntimeError("download failed")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=YoutubeDL))

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        source = SourceConfig(
            key="website:hls",
            template="website",
            name="HLS",
            enabled=True,
            url="https://source.example/page",
            content_types=(ContentType.VIDEO,),
            command="/hls",
            video_quality="lowest",
        )
        candidate = Candidate(
            ContentType.VIDEO,
            url="https://cdn.example/video.m3u8",
            referer=source.url,
        )
        with pytest.raises(Exception, match="HLS 视频下载失败"):
            await pipeline._materialize_hls(source, candidate)
        assert attempts == [
            ("worst", {}),
            (None, {}),
            ("worst", {"Referer": source.url}),
            (None, {"Referer": source.url}),
        ]
        assert list(pipeline.cache.files_dir.glob(".hls-*")) == []
        await pipeline.close()

    asyncio.run(scenario())


def test_pipeline_crawls_video_detail_pages(tmp_path: Path) -> None:
    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        pipeline.fetcher = FakeVideoFetcher()  # type: ignore[assignment]
        source = SourceConfig(
            key="website:video",
            template="website",
            name="Video",
            enabled=True,
            url="https://avbebe.com/archives/category/video",
            content_types=(ContentType.VIDEO,),
            command="/video",
            dedupe=-1,
            rate_limit=-1,
        )
        asset = await pipeline.collect(source, None, "user")
        assert asset.exists
        assert asset.content_type is ContentType.VIDEO
        assert asset.origin_url == "https://cdn.example/7.mp4"
        await pipeline.close()

    asyncio.run(scenario())


def test_pipeline_defers_embed_fetch_when_detail_media_succeeds(tmp_path: Path) -> None:
    class DirectDetailFetcher:
        def __init__(self) -> None:
            self.fetches: list[str] = []

        async def fetch_source(self, source):
            return FetchResponse(
                source.url,
                200,
                "text/html",
                b"<a href='/video/1'>video</a>",
            )

        async def fetch(self, url, *, headers=None):
            self.fetches.append(url)
            if url == "https://source.example/video/1":
                return FetchResponse(
                    url,
                    200,
                    "text/html",
                    b"<video src='https://cdn.example/direct.mp4'></video>"
                    b"<iframe src='https://player.example/embed/1'></iframe>",
                )
            raise AssertionError("embed page must stay deferred after direct media succeeds")

        async def download(self, url, destination, *, headers=None):
            body = b"direct-video"
            destination.write_bytes(body)
            return DownloadedFile(
                url,
                200,
                "video/mp4",
                {},
                destination,
                hashlib.sha256(body).hexdigest(),
                len(body),
            )

        async def close(self) -> None:
            return None

    async def scenario() -> None:
        pipeline = CollectorPipeline(tmp_path)
        await pipeline.initialize()
        await pipeline.fetcher.close()
        fake = DirectDetailFetcher()
        pipeline.fetcher = fake  # type: ignore[assignment]
        source = SourceConfig(
            key="website:lazy-embed",
            template="website",
            name="Lazy embed",
            enabled=True,
            url="https://source.example/list",
            content_types=(ContentType.VIDEO,),
            command="/video",
            dedupe=-1,
            rate_limit=-1,
        )
        asset = await pipeline.collect(source, None, "user")
        assert asset.origin_url == "https://cdn.example/direct.mp4"
        assert fake.fetches == ["https://source.example/video/1"]
        await pipeline.close()

    asyncio.run(scenario())
