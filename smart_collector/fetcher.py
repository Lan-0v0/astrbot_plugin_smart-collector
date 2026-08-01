from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import random
import shutil
import socket
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .models import DownloadedFile, FetchResponse, SourceConfig

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

RETRYABLE_STATUS = {403, 408, 425, 429, 500, 502, 503, 504}
MIN_FREE_DISK_BYTES = 128 * 1024 * 1024
CHALLENGE_MARKERS = (
    b"cf-chl-",
    b"cloudflare ray id",
    b"just a moment",
    b"checking your browser",
)
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 10


class FetchError(RuntimeError):
    pass


class AntiBotFetcher:
    """Async fetcher with retries and TLS/browser impersonation fallback.

    This handles ordinary JavaScript-less Cloudflare interstitials and TLS
    fingerprint checks. It intentionally does not defeat CAPTCHAs, logins, or
    other access controls.
    """

    def __init__(
        self,
        *,
        timeout: float = -1,
        max_bytes: int = 100 * 1024 * 1024,
        concurrency: int = -1,
    ) -> None:
        self.timeout = None if timeout < 0 else timeout
        self.max_bytes = max_bytes
        self._semaphore = asyncio.Semaphore(max(1, concurrency)) if concurrency >= 0 else None
        self._impersonated_max_clients = max(1, concurrency) if concurrency >= 0 else 32
        self._impersonated_session: Any | None = None
        self._impersonated_session_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=self.timeout,
            headers=DEFAULT_HEADERS,
            http2=True,
        )

    async def close(self) -> None:
        session = self._impersonated_session
        self._impersonated_session = None
        if session is not None:
            with suppress(Exception):
                await session.close()
        await self._client.aclose()

    async def _get_impersonated_session(self) -> Any | None:
        if self._impersonated_session is not None:
            return self._impersonated_session
        async with self._impersonated_session_lock:
            if self._impersonated_session is not None:
                return self._impersonated_session
            try:
                from curl_cffi.requests import AsyncSession
            except ImportError:
                return None
            session_options = {
                "impersonate": "chrome",
                "max_clients": self._impersonated_max_clients,
            }
            if self.timeout is not None:
                session_options["timeout"] = self.timeout
            self._impersonated_session = AsyncSession(**session_options)
            return self._impersonated_session

    async def fetch_source(self, source: SourceConfig) -> FetchResponse:
        headers = self.source_headers(source)
        response = await self.fetch(source.url, headers=headers)
        if source.template == "api" and self._api_key_rejected(response):
            key_pair = next(
                ((key, value) for key, value in source.headers.items() if key.lower() == "key"),
                None,
            )
            if key_pair:
                response = await self.fetch(
                    self._with_query(source.url, *key_pair), headers=headers
                )
        return response

    @staticmethod
    def source_headers(source: SourceConfig) -> dict[str, str]:
        headers = dict(source.headers)
        if source.cookies:
            headers["Cookie"] = random.choice(source.cookies)
        return headers

    async def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        await self._validate_network_url(url)
        if self._semaphore is None:
            return await self._fetch_with_retries(url, headers or {})
        async with self._semaphore:
            return await self._fetch_with_retries(url, headers or {})

    async def download(
        self, url: str, destination: Path, *, headers: dict[str, str] | None = None
    ) -> DownloadedFile:
        """Stream a media response to disk without the page-response memory limit."""
        await self._validate_network_url(url)
        if self._semaphore is None:
            return await self._download_with_retries(url, destination, headers or {})
        async with self._semaphore:
            return await self._download_with_retries(url, destination, headers or {})

    async def probe(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> FetchResponse | None:
        """Perform a short HEAD request used only to rank media candidates."""
        await self._validate_network_url(url)

        async def request() -> FetchResponse | None:
            timeout = 15.0 if self.timeout is None else min(15.0, max(0.1, self.timeout))
            result = await self._httpx_probe_request("HEAD", url, headers or {}, timeout)
            if result is None:
                return None
            if result.status not in {405, 501} and result.content_type:
                return result
            range_headers = {**(headers or {}), "Range": "bytes=0-0"}
            return await self._httpx_probe_request("GET", url, range_headers, timeout) or result

        if self._semaphore is None:
            return await request()
        async with self._semaphore:
            return await request()

    async def _httpx_probe_request(
        self, method: str, url: str, headers: dict[str, str], timeout: float
    ) -> FetchResponse | None:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            await self._validate_network_url(current)
            try:
                async with self._client.stream(
                    method, current, headers=headers, timeout=timeout
                ) as response:
                    location = response.headers.get("location")
                    if response.status_code in REDIRECT_STATUSES and location:
                        current = self._validated_redirect(current, location)
                        continue
                    final_url = str(response.url)
                    self._validate_url(final_url)
                    return FetchResponse(
                        url=final_url,
                        status=response.status_code,
                        content_type=response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .lower(),
                        body=b"",
                        headers=dict(response.headers),
                        transport="httpx-head" if method == "HEAD" else "httpx-range-probe",
                    )
            except httpx.HTTPError:
                return None
        raise FetchError(f"重定向次数超过 {MAX_REDIRECTS}: {url}")

    async def _fetch_with_retries(self, url: str, headers: dict[str, str]) -> FetchResponse:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._httpx_fetch(url, headers)
                if response.status not in RETRYABLE_STATUS and not self._is_challenge(response):
                    return response
                if attempt == 0:
                    impersonated = await self._impersonated_fetch(url, headers)
                    if (
                        impersonated
                        and impersonated.status < 400
                        and not self._is_challenge(impersonated)
                    ):
                        return impersonated
                last_error = FetchError(f"HTTP {response.status}: {url}")
            except (httpx.HTTPError, OSError, FetchError) as exc:
                last_error = exc
                if attempt == 0:
                    impersonated = await self._impersonated_fetch(url, headers)
                    if (
                        impersonated
                        and impersonated.status < 400
                        and not self._is_challenge(impersonated)
                    ):
                        return impersonated
            if attempt < 2:
                await asyncio.sleep(0.5 * (2**attempt) + random.random() * 0.25)
        raise FetchError(f"抓取失败 {url}: {last_error!r}") from last_error

    async def _httpx_fetch(
        self, url: str, headers: dict[str, str], _redirects: int = 0
    ) -> FetchResponse:
        await self._validate_network_url(url)
        async with self._client.stream("GET", url, headers=headers) as response:
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if not location:
                    self._validate_url(str(response.url))
                elif _redirects >= MAX_REDIRECTS:
                    raise FetchError(f"重定向次数超过 {MAX_REDIRECTS}: {url}")
                else:
                    target = self._validated_redirect(url, location)
                    return await self._httpx_fetch(target, headers, _redirects + 1)
            self._validate_url(str(response.url))
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self.max_bytes:
                    raise FetchError(f"响应超过 {self.max_bytes} 字节上限")
            return FetchResponse(
                url=str(response.url),
                status=response.status_code,
                content_type=response.headers.get("content-type", "").split(";", 1)[0].lower(),
                body=bytes(body),
                headers=dict(response.headers),
                transport="httpx",
            )

    async def _download_with_retries(
        self, url: str, destination: Path, headers: dict[str, str]
    ) -> DownloadedFile:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                result = await self._httpx_download(url, destination, headers)
                if result.status not in RETRYABLE_STATUS and not self._download_is_challenge(
                    result
                ):
                    return result
                if attempt == 0:
                    impersonated = await self._impersonated_download(url, destination, headers)
                    if (
                        impersonated
                        and impersonated.status < 400
                        and not self._download_is_challenge(impersonated)
                    ):
                        return impersonated
                last_error = FetchError(f"HTTP {result.status}: {url}")
            except (httpx.HTTPError, OSError, FetchError) as exc:
                last_error = exc
                if attempt == 0:
                    impersonated = await self._impersonated_download(url, destination, headers)
                    if (
                        impersonated
                        and impersonated.status < 400
                        and not self._download_is_challenge(impersonated)
                    ):
                        return impersonated
            if attempt < 2:
                await asyncio.sleep(0.5 * (2**attempt) + random.random() * 0.25)
        destination.unlink(missing_ok=True)
        raise FetchError(f"下载失败 {url}: {last_error!r}") from last_error

    async def _httpx_download(
        self, url: str, destination: Path, headers: dict[str, str], _redirects: int = 0
    ) -> DownloadedFile:
        await self._validate_network_url(url)
        digest = hashlib.sha256()
        size = 0
        try:
            async with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        self._validate_url(str(response.url))
                    elif _redirects >= MAX_REDIRECTS:
                        raise FetchError(f"重定向次数超过 {MAX_REDIRECTS}: {url}")
                    else:
                        target = self._validated_redirect(url, location)
                        return await self._httpx_download(
                            target, destination, headers, _redirects + 1
                        )
                self._validate_url(str(response.url))
                self._ensure_disk_capacity(destination, response.headers)
                with destination.open("wb") as stream:
                    async for chunk in response.aiter_bytes():
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if size % (256 * 1024 * 1024) < len(chunk):
                            self._ensure_disk_capacity(destination, {})
                return DownloadedFile(
                    url=str(response.url),
                    status=response.status_code,
                    content_type=response.headers.get("content-type", "").split(";", 1)[0].lower(),
                    headers=dict(response.headers),
                    local_path=destination,
                    sha256=digest.hexdigest(),
                    size=size,
                    transport="httpx",
                )
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    async def _impersonated_fetch(
        self, url: str, headers: dict[str, str], _redirects: int = 0
    ) -> FetchResponse | None:
        await self._validate_network_url(url)
        session = await self._get_impersonated_session()
        if session is None:
            return None
        response = None
        try:
            response = await session.get(
                url, headers={**DEFAULT_HEADERS, **headers}, allow_redirects=False
            )
            if int(response.status_code) in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if location and _redirects < MAX_REDIRECTS:
                    target = self._validated_redirect(url, str(location))
                    await response.aclose()
                    response = None
                    return await self._impersonated_fetch(target, headers, _redirects + 1)
                if location:
                    raise FetchError(f"重定向次数超过 {MAX_REDIRECTS}: {url}")
            self._validate_url(str(response.url))
            body = bytes(response.content)
            if len(body) > self.max_bytes:
                raise FetchError(f"响应超过 {self.max_bytes} 字节上限")
            return FetchResponse(
                url=str(response.url),
                status=int(response.status_code),
                content_type=str(response.headers.get("content-type", "")).split(";", 1)[0].lower(),
                body=body,
                headers={str(k): str(v) for k, v in response.headers.items()},
                transport="curl_cffi",
            )
        except Exception:
            return None
        finally:
            if response is not None:
                with suppress(Exception):
                    await response.aclose()

    async def _impersonated_download(
        self, url: str, destination: Path, headers: dict[str, str], _redirects: int = 0
    ) -> DownloadedFile | None:
        await self._validate_network_url(url)
        session = await self._get_impersonated_session()
        if session is None:
            return None
        digest = hashlib.sha256()
        size = 0
        response = None
        try:
            response = await session.get(
                url,
                headers={**DEFAULT_HEADERS, **headers},
                allow_redirects=False,
                stream=True,
            )
            if int(response.status_code) in REDIRECT_STATUSES:
                location = response.headers.get("location")
                if location and _redirects < MAX_REDIRECTS:
                    target = self._validated_redirect(url, str(location))
                    await response.aclose()
                    response = None
                    return await self._impersonated_download(
                        target, destination, headers, _redirects + 1
                    )
                if location:
                    raise FetchError(f"重定向次数超过 {MAX_REDIRECTS}: {url}")
            self._validate_url(str(response.url))
            self._ensure_disk_capacity(destination, response.headers)
            with destination.open("wb") as stream:
                async for chunk in response.aiter_content():
                    if not chunk:
                        continue
                    stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if size % (256 * 1024 * 1024) < len(chunk):
                        self._ensure_disk_capacity(destination, {})
            return DownloadedFile(
                url=str(response.url),
                status=int(response.status_code),
                content_type=str(response.headers.get("content-type", "")).split(";", 1)[0].lower(),
                headers={str(k): str(v) for k, v in response.headers.items()},
                local_path=destination,
                sha256=digest.hexdigest(),
                size=size,
                transport="curl_cffi",
            )
        except Exception:
            destination.unlink(missing_ok=True)
            return None
        finally:
            if response is not None:
                await response.aclose()

    @classmethod
    def _validate_url(cls, url: str) -> None:
        parts = urlsplit(url)
        try:
            _ = parts.hostname
            _ = parts.port
        except ValueError as exc:
            raise FetchError("仅支持有效的 HTTP/HTTPS URL") from exc
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise FetchError("仅支持有效的 HTTP/HTTPS URL")
        hostname = parts.hostname.lower().rstrip(".")
        address = AntiBotFetcher._parse_ip_literal(hostname)
        if address is not None and cls._is_blocked_address(address):
            raise FetchError("不允许抓取本机或内网地址")
        if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
            raise FetchError("不允许抓取本机地址")

    @classmethod
    async def _validate_network_url(cls, url: str) -> None:
        """Validate the URL syntax and reject hostnames resolving to local IPs."""
        cls._validate_url(url)
        parts = urlsplit(url)
        hostname = parts.hostname
        if not hostname or cls._parse_ip_literal(hostname) is not None:
            return

        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror:
            return
        except OSError as exc:
            raise FetchError("无法解析目标地址") from exc

        for address_info in addresses:
            socket_address = address_info[4]
            try:
                address = ipaddress.ip_address(socket_address[0])
            except (IndexError, ValueError, TypeError) as exc:
                raise FetchError("无法解析目标地址") from exc
            if cls._is_blocked_address(address):
                raise FetchError("不允许抓取本机或内网地址")

    @staticmethod
    def _is_blocked_address(
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> bool:
        return any(
            (
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_multicast,
                address.is_reserved,
                address.is_unspecified,
            )
        )

    @classmethod
    def _validated_redirect(cls, current_url: str, location: str) -> str:
        target = urljoin(current_url, location.strip())
        cls._validate_url(target)
        return target

    @staticmethod
    def _parse_ip_literal(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(hostname)
        except ValueError:
            pass
        try:
            if hostname.lower().startswith("0x"):
                value = int(hostname, 16)
                if 0 <= value <= 0xFFFFFFFF:
                    return ipaddress.IPv4Address(value)
            if hostname.isdigit() and len(hostname) <= 10:
                value = int(hostname, 10)
                if 0 <= value <= 0xFFFFFFFF:
                    return ipaddress.IPv4Address(value)
            parts = hostname.split(".")
            if len(parts) == 4 and all(part.isdigit() for part in parts):
                octets = [int(part, 10) for part in parts]
                if all(0 <= part <= 255 for part in octets):
                    return ipaddress.IPv4Address(".".join(str(part) for part in octets))
        except (TypeError, ValueError, OverflowError):
            return None
        return None

    @staticmethod
    def _is_challenge(response: FetchResponse) -> bool:
        sample = response.body[:256_000].lower()
        return any(marker in sample for marker in CHALLENGE_MARKERS)

    @staticmethod
    def _download_is_challenge(response: DownloadedFile) -> bool:
        if response.content_type not in {"text/html", "application/xhtml+xml"}:
            return False
        try:
            with response.local_path.open("rb") as stream:
                sample = stream.read(256_000).lower()
        except OSError:
            return False
        return any(marker in sample for marker in CHALLENGE_MARKERS)

    @staticmethod
    def _ensure_disk_capacity(destination: Path, headers: Any) -> None:
        free = shutil.disk_usage(destination.parent).free
        raw_length = headers.get("content-length", "") if headers else ""
        try:
            content_length = int(raw_length or 0)
        except (TypeError, ValueError):
            content_length = 0
        required = content_length + MIN_FREE_DISK_BYTES
        if free < required:
            raise FetchError(
                f"磁盘空间不足：响应需要约 {content_length} 字节，当前可用 {free} 字节"
            )

    @staticmethod
    def _api_key_rejected(response: FetchResponse) -> bool:
        if "json" not in response.content_type:
            return False
        try:
            payload: Any = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        try:
            code = int(payload.get("code", 0) or 0)
        except (TypeError, ValueError):
            return False
        return code in {401, 403}

    @staticmethod
    def _with_query(url: str, key: str, value: str) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query[key] = value
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
