import asyncio
import json
from pathlib import Path

import curl_cffi.requests
import httpx

from smart_collector.fetcher import AntiBotFetcher
from smart_collector.models import FetchResponse


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


def test_non_numeric_api_code_is_not_treated_as_rejected() -> None:
    response = FetchResponse(
        "https://api.example/data",
        200,
        "application/json",
        json.dumps({"code": "Unauthorized"}).encode(),
    )
    assert not AntiBotFetcher._api_key_rejected(response)


def test_probe_falls_back_to_range_get_when_head_is_not_supported() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.headers.get("range", "")))
        if request.method == "HEAD":
            return httpx.Response(405, request=request)
        return httpx.Response(
            206,
            headers={"content-type": "video/mp4", "content-range": "bytes 0-0/1000"},
            content=b"x",
            request=request,
        )

    async def scenario() -> None:
        fetcher = AntiBotFetcher(timeout=2)
        await fetcher._client.aclose()
        fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await fetcher.probe("https://cdn.example/play?id=1")
            assert result and result.status == 206
            assert result.content_type == "video/mp4"
            assert result.transport == "httpx-range-probe"
            assert requests == [("HEAD", ""), ("GET", "bytes=0-0")]
        finally:
            await fetcher.close()

    asyncio.run(scenario())


def test_browser_impersonation_session_is_reused_and_closed(monkeypatch) -> None:
    sessions = []

    class FakeResponse:
        def __init__(self) -> None:
            self.url = "https://example.com/page"
            self.status_code = 200
            self.headers = {"content-type": "text/html"}
            self.content = b"page"

        async def aclose(self) -> None:
            return None

    class FakeSession:
        def __init__(self, **options) -> None:
            self.options = options
            self.closed = False
            self.requests = 0
            sessions.append(self)

        async def get(self, *args, **kwargs):
            self.requests += 1
            return FakeResponse()

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", FakeSession)

    async def scenario() -> None:
        fetcher = AntiBotFetcher(timeout=8)
        first = await fetcher._impersonated_fetch("https://example.com/one", {})
        second = await fetcher._impersonated_fetch("https://example.com/two", {})
        assert first and second
        assert len(sessions) == 1
        assert sessions[0].requests == 2
        assert sessions[0].options == {
            "impersonate": "chrome",
            "max_clients": 32,
            "timeout": 8,
        }
        await fetcher.close()
        assert sessions[0].closed

    asyncio.run(scenario())
