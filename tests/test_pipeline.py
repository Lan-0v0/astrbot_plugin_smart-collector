import asyncio
from pathlib import Path

from smart_collector.models import ContentType, FetchResponse, SourceConfig
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
        self.media_fetches += 1
        return FetchResponse(url=url, status=200, content_type="image/png", body=b"png-data")

    async def close(self) -> None:
        return None


class FakeVideoFetcher:
    async def fetch_source(self, source: SourceConfig) -> FetchResponse:
        return FetchResponse(
            url=source.url,
            status=200,
            content_type="text/html",
            body=b"<html><a class='block' href='/movie/7'>video</a></html>",
        )

    async def fetch(self, url: str, *, headers=None) -> FetchResponse:
        if "/movie/" in url:
            return FetchResponse(
                url=url,
                status=200,
                content_type="text/html",
                body=b"""<script type="application/ld+json">{"@type":"VideoObject","contentUrl":"https://cdn.example/7.mp4"}</script>""",
            )
        return FetchResponse(url=url, status=200, content_type="video/mp4", body=b"video-data")

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
            url="https://twitter-ero-video-ranking.com",
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
