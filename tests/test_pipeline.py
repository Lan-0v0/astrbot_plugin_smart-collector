import asyncio
import hashlib
from pathlib import Path

from smart_collector.models import (
    Candidate,
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
        return FetchResponse(url=url, status=200, content_type="image/png", body=b"png-data")

    async def download(self, url: str, destination: Path, *, headers=None) -> DownloadedFile:
        self.media_fetches += 1
        body = b"png-data"
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
        pipeline = CollectorPipeline(tmp_path)
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
