import asyncio
from pathlib import Path

import httpx

from smart_collector.fetcher import AntiBotFetcher


def test_negative_limits_disable_timeout_and_concurrency_limit() -> None:
    async def scenario() -> None:
        fetcher = AntiBotFetcher(concurrency=-1, timeout=-1)
        try:
            assert fetcher._semaphore is None
            assert fetcher.timeout is None
        finally:
            await fetcher.close()

    asyncio.run(scenario())


def test_nonnegative_concurrency_still_uses_a_semaphore() -> None:
    async def scenario() -> None:
        fetcher = AntiBotFetcher(concurrency=3, timeout=10)
        try:
            assert fetcher._semaphore is not None
            assert fetcher._semaphore._value == 3
            assert fetcher.timeout == 10
        finally:
            await fetcher.close()

    asyncio.run(scenario())


def test_media_download_streams_past_page_buffer_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        fetcher = AntiBotFetcher(max_bytes=1)
        await fetcher._client.aclose()
        fetcher._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"content-type": "video/mp4"},
                    content=b"streamed-video",
                    request=request,
                )
            )
        )
        target = tmp_path / "video.part"
        try:
            result = await fetcher.download("https://cdn.example/video.mp4", target)
            assert result.size == len(b"streamed-video")
            assert target.read_bytes() == b"streamed-video"
        finally:
            await fetcher.close()

    asyncio.run(scenario())
