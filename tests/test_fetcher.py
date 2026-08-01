import asyncio
import json
import socket
from pathlib import Path

import curl_cffi.requests
import httpx

from smart_collector.fetcher import AntiBotFetcher, FetchError
from smart_collector.models import FetchResponse


def _allow_public_test_dns(monkeypatch) -> None:
    def fake_getaddrinfo(hostname, port, *, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr("smart_collector.fetcher.socket.getaddrinfo", fake_getaddrinfo)


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


def test_private_and_special_ip_urls_are_rejected() -> None:
    for url in (
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://2130706433/",
        "http://0x7f000001/",
    ):
        try:
            AntiBotFetcher._validate_url(url)
        except FetchError:
            continue
        raise AssertionError(f"private URL was accepted: {url}")


def test_hostname_resolving_to_private_ip_is_rejected(monkeypatch) -> None:
    def fake_getaddrinfo(hostname, port, *, type):
        assert hostname == "public.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]

    monkeypatch.setattr("smart_collector.fetcher.socket.getaddrinfo", fake_getaddrinfo)

    async def scenario() -> None:
        fetcher = AntiBotFetcher()
        try:
            try:
                await fetcher.fetch("https://public.example/")
            except FetchError as exc:
                assert "内网" in str(exc)
            else:
                raise AssertionError("hostname resolving to a private IP was accepted")
        finally:
            await fetcher.close()

    asyncio.run(scenario())


def test_redirect_to_private_ip_is_rejected(monkeypatch, tmp_path: Path) -> None:
    _allow_public_test_dns(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/admin"},
            request=request,
        )

    async def scenario() -> None:
        fetcher = AntiBotFetcher()
        await fetcher._client.aclose()
        fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            try:
                await fetcher.fetch("https://example.com/start")
            except FetchError as exc:
                assert "内网" in str(exc) or "本机" in str(exc)
            else:
                raise AssertionError("redirect to private IP was accepted")
        finally:
            await fetcher.close()

    asyncio.run(scenario())


def test_httpx_redirects_are_followed_only_after_validation(monkeypatch) -> None:
    _allow_public_test_dns(monkeypatch)

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"}, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"ok",
            request=request,
        )

    async def scenario() -> None:
        fetcher = AntiBotFetcher()
        await fetcher._client.aclose()
        fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await fetcher.fetch("https://example.com/start")
            assert result.status == 200
            assert result.body == b"ok"
            assert requests == ["https://example.com/start", "https://example.com/final"]
        finally:
            await fetcher.close()

    asyncio.run(scenario())


def test_media_download_streams_past_page_buffer_limit(monkeypatch, tmp_path: Path) -> None:
    _allow_public_test_dns(monkeypatch)

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


def test_probe_falls_back_to_range_get_when_head_is_not_supported(monkeypatch) -> None:
    _allow_public_test_dns(monkeypatch)

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
    _allow_public_test_dns(monkeypatch)

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
