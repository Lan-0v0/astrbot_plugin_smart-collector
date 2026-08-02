from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from .models import Candidate, ContentType, SourceConfig


class PixivError(RuntimeError):
    pass


PIXIV_LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
PIXIV_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
PIXIV_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
PIXIV_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
PIXIV_USER_AGENT = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
PIXIV_TOKEN_FILE = "pixiv_auth.json"
PIXIV_LOCAL_LOGIN_TIMEOUT = 600


@dataclass(slots=True)
class PendingPixivLogin:
    verifier: str
    state: str
    created_at: float


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _pixiv_image_urls(illust: Any, quality: str = "original") -> list[tuple[str, int, int]]:
    urls: list[tuple[str, int, int]] = []
    seen_urls: set[str] = set()

    def append_image_urls(image_urls: Any, original_url: Any, width: int, height: int) -> None:
        available_urls = {
            "original": str(original_url or "").strip(),
            "large": str(_value(image_urls, "large", "") or "").strip(),
            "medium": str(_value(image_urls, "medium", "") or "").strip(),
        }
        selected_url = available_urls.get(quality, "")
        if not selected_url or selected_url in seen_urls:
            return
        seen_urls.add(selected_url)
        urls.append((selected_url, width, height))

    width = int(_value(illust, "width", 0) or 0)
    height = int(_value(illust, "height", 0) or 0)
    pages = _value(illust, "meta_pages", []) or []
    for page in pages:
        image_urls = _value(page, "image_urls", {}) or {}
        append_image_urls(
            image_urls,
            _value(image_urls, "original", ""),
            width,
            height,
        )
    if not pages:
        single = _value(illust, "meta_single_page", {}) or {}
        append_image_urls(
            _value(illust, "image_urls", {}) or {},
            _value(single, "original_image_url", ""),
            width,
            height,
        )
    return urls


def _oauth_callback_params(value: str) -> dict[str, list[str]]:
    current = str(value or "").strip()
    for _ in range(4):
        parts = urlsplit(current)
        params = parse_qs(parts.query) if parts.scheme else {"code": [current]}
        if params.get("code") or params.get("error") or params.get("error_description"):
            return params
        nested = (params.get("return_to") or params.get("redirect_uri") or [""])[0]
        if not nested or nested == current:
            return params
        current = nested
    return {}


def _callback_from_cdp_payload(value: Any) -> str:
    if isinstance(value, str):
        if value.startswith("pixiv://") and _oauth_callback_params(value).get("code"):
            return value
        return ""
    if isinstance(value, dict):
        for item in value.values():
            callback = _callback_from_cdp_payload(item)
            if callback:
                return callback
    elif isinstance(value, list):
        for item in value:
            callback = _callback_from_cdp_payload(item)
            if callback:
                return callback
    return ""


def _find_local_browser() -> Path | None:
    candidates: list[Path] = []
    for variable, suffixes in (
        (
            "LOCALAPPDATA",
            (
                "Google/Chrome/Application/chrome.exe",
                "Microsoft/Edge/Application/msedge.exe",
            ),
        ),
        (
            "PROGRAMFILES",
            (
                "Google/Chrome/Application/chrome.exe",
                "Microsoft/Edge/Application/msedge.exe",
            ),
        ),
        (
            "PROGRAMFILES(X86)",
            (
                "Google/Chrome/Application/chrome.exe",
                "Microsoft/Edge/Application/msedge.exe",
            ),
        ),
    ):
        root = os.environ.get(variable)
        if root:
            candidates.extend(Path(root) / suffix for suffix in suffixes)
    candidates.extend(
        Path(item)
        for item in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
        )
    )
    for command in ("chrome", "google-chrome", "chromium", "msedge", "microsoft-edge"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    return next((path for path in candidates if path.is_file()), None)


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def parse_pixiv_query(query: str, configured_age: str = "all") -> tuple[str, str]:
    text = str(query or "").strip()
    age = configured_age if configured_age in {"all", "safe", "r18"} else "all"
    lowered = text.casefold()
    if re.search(r"(?i)(?:r[- ]?18|成人|涩图|色图)", lowered):
        age = "r18"
    elif any(marker in lowered for marker in ("全年龄", "全年齡", "sfw")):
        age = "safe"

    text = re.sub(r"(?i)(?:r[- ]?18|全年龄|全年齡|sfw|成人|涩图|色图)", " ", text)
    text = re.sub(
        r"(?:帮我|請幫我|请帮我|在p站上|在P站上|在pixiv上|找一下|搜索|搜寻|查找|找|图片|圖片|插画|插畫|作品|的)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("，", ",").replace("、", ",")
    try:
        pieces = shlex.split(text)
    except ValueError:
        pieces = text.split()
    tags: list[str] = []
    for piece in pieces:
        for tag in piece.split(","):
            tag = tag.strip().strip("#")
            tag = re.sub(r"(?:图|圖|图片|圖片|插画|插畫)$", "", tag).strip()
            if tag and tag not in tags:
                tags.append(tag)
    if not tags:
        raise PixivError("Pixiv 请提供至少一个 Tag，例如：/pixiv 百合 JK 白丝")
    return " ".join(tags), age


class PixivAuthManager:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / PIXIV_TOKEN_FILE
        self.pending: PendingPixivLogin | None = None
        self._local_login_lock = asyncio.Lock()

    async def start(self) -> str:
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(24)
        self.pending = PendingPixivLogin(verifier, state, datetime.now(timezone.utc).timestamp())
        params = {
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "client": "pixiv-android",
            "state": state,
        }
        return f"{PIXIV_LOGIN_URL}?{urlencode(params)}"

    async def finish(self, callback: str) -> None:
        pending = self.pending
        if pending is None or datetime.now(timezone.utc).timestamp() - pending.created_at > 600:
            raise PixivError("Pixiv 登录授权已过期，请重新发起登录")
        value = str(callback or "").strip()
        params = _oauth_callback_params(value)
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        if not code:
            error = (params.get("error_description") or params.get("error") or [""])[0]
            raise PixivError(error or "未找到授权 code")
        if state and state != pending.state:
            raise PixivError("Pixiv 登录回调 state 校验失败")

        import httpx

        payload = {
            "client_id": PIXIV_CLIENT_ID,
            "client_secret": PIXIV_CLIENT_SECRET,
            "code": code,
            "code_verifier": pending.verifier,
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": PIXIV_REDIRECT_URI,
        }
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": PIXIV_USER_AGENT},
        ) as client:
            response = await client.post(PIXIV_TOKEN_URL, data=payload)
        if response.status_code >= 400:
            raise PixivError(f"Pixiv Token 交换失败：HTTP {response.status_code}")
        data = response.json()
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not refresh_token:
            raise PixivError("Pixiv Token 交换成功但未返回 Refresh Token")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            self.path.write_text,
            json.dumps(
                {
                    "refresh_token": refresh_token,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            ),
            "utf-8",
        )
        self.pending = None

    async def login_local(self) -> None:
        if self._local_login_lock.locked():
            raise PixivError("已有 Pixiv 本地登录窗口正在等待授权")
        async with self._local_login_lock:
            login_url = await self.start()
            callback = await self._capture_local_callback(login_url)
            await self.finish(callback)

    async def _capture_local_callback(self, login_url: str) -> str:
        browser = _find_local_browser()
        if browser is None:
            raise PixivError("未找到 Chrome 或 Edge，无法使用本地自动登录")
        port = _free_local_port()
        profile = self.path.parent / "pixiv_browser_profile"
        profile.mkdir(parents=True, exist_ok=True)
        command = [
            str(browser),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--incognito",
            "--new-window",
            "about:blank",
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = await asyncio.to_thread(
                subprocess.Popen,
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise PixivError(f"无法启动本地浏览器：{exc}") from exc

        websocket_url = ""
        try:
            websocket_url = await self._wait_for_page(port, process)
            return await self._listen_for_callback(websocket_url, login_url)
        finally:
            if websocket_url:
                await self._close_browser(websocket_url)
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                with suppress(ProcessLookupError, subprocess.TimeoutExpired):
                    await asyncio.to_thread(process.wait, 5)
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.kill()

    @staticmethod
    async def _wait_for_page(port: int, process: subprocess.Popen) -> str:
        import httpx

        deadline = asyncio.get_running_loop().time() + 20
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            while asyncio.get_running_loop().time() < deadline:
                if process.poll() is not None:
                    raise PixivError("本地浏览器启动后意外退出")
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/json/list")
                    for target in response.json():
                        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                            return str(target["webSocketDebuggerUrl"])
                except (httpx.HTTPError, ValueError, TypeError):
                    pass
                await asyncio.sleep(0.2)
        raise PixivError("连接本地浏览器超时")

    @staticmethod
    async def _listen_for_callback(websocket_url: str, login_url: str) -> str:
        import websockets

        async with websockets.connect(
            websocket_url,
            open_timeout=10,
            close_timeout=2,
            max_size=2 * 1024 * 1024,
        ) as connection:
            command_id = 0
            for method, params in (
                ("Page.enable", {}),
                ("Network.enable", {}),
                ("Page.navigate", {"url": login_url}),
            ):
                command_id += 1
                await connection.send(
                    json.dumps({"id": command_id, "method": method, "params": params})
                )
            deadline = asyncio.get_running_loop().time() + PIXIV_LOCAL_LOGIN_TIMEOUT
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise PixivError("本地 Pixiv 登录已超时，请重新发送 /pixiv本地登陆")
                try:
                    message = await asyncio.wait_for(connection.recv(), min(remaining, 1.0))
                except TimeoutError:
                    command_id += 1
                    await connection.send(
                        json.dumps({"id": command_id, "method": "Page.getNavigationHistory"})
                    )
                    continue
                try:
                    payload = json.loads(message)
                except (json.JSONDecodeError, TypeError):
                    continue
                callback = _callback_from_cdp_payload(payload)
                if callback:
                    return callback

    @staticmethod
    async def _close_browser(websocket_url: str) -> None:
        import websockets

        try:
            async with websockets.connect(
                websocket_url, open_timeout=2, close_timeout=1
            ) as connection:
                await connection.send(json.dumps({"id": 1, "method": "Browser.close"}))
                with suppress(Exception):
                    await asyncio.wait_for(connection.recv(), 2)
        except Exception:
            pass

    async def load_refresh_token(self) -> str:
        try:
            data = await asyncio.to_thread(self.path.read_text, "utf-8")
            value = json.loads(data).get("refresh_token", "")
            return str(value).strip()
        except (OSError, json.JSONDecodeError, AttributeError):
            return ""


class PixivCollector:
    def __init__(self, data_dir: Path) -> None:
        self.auth = PixivAuthManager(data_dir)
        self._clients: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._authenticated: set[str] = set()

    async def candidates(self, source: SourceConfig, query: str) -> list[Candidate]:
        token = source.pixiv_refresh_token or await self.auth.load_refresh_token()
        if not token:
            raise PixivError(
                "Pixiv 未配置 Refresh Token，请先使用 /pixiv本地登陆 或 /pixiv远程登陆"
            )
        word, age = parse_pixiv_query(query, source.pixiv_age_mode)
        client = await self._client(token)
        lock = self._locks[token]
        async with lock:
            try:
                if token not in self._authenticated:
                    await asyncio.to_thread(client.auth, refresh_token=token)
                    self._authenticated.add(token)
                illusts = await self._search(client, word)
            except Exception as exc:
                self._authenticated.discard(token)
                raise PixivError(f"Pixiv 搜索失败：{exc}") from exc

        candidates: list[Candidate] = []
        seen: set[str] = set()
        for illust in illusts:
            restrict = int(_value(illust, "x_restrict", 0) or 0)
            if age == "safe" and restrict != 0:
                continue
            if age == "r18" and restrict == 0:
                continue
            title = str(_value(illust, "title", "") or "")
            tags = " ".join(
                str(_value(tag, "name", "") or "") for tag in (_value(illust, "tags", []) or [])
            )
            image_urls = _pixiv_image_urls(illust, source.pixiv_quality)
            illust_id = str(_value(illust, "id", "") or "").strip()
            if not illust_id and image_urls:
                illust_id = hashlib.sha1(image_urls[0][0].encode()).hexdigest()[:20]
            group_key = f"pixiv:{illust_id}"
            for page_index, (url, width, height) in enumerate(image_urls):
                if url in seen:
                    continue
                seen.add(url)
                candidates.append(
                    Candidate(
                        ContentType.IMAGE,
                        url=url,
                        title=title,
                        referer="https://www.pixiv.net/",
                        width=width,
                        height=height,
                        context_text=tags,
                        source_kind="pixiv_api",
                        group_key=group_key,
                        page_index=page_index,
                        r18=restrict != 0,
                    )
                )
        if not candidates:
            quality_labels = {
                "original": "原图",
                "large": "大图",
                "medium": "中图",
            }
            quality_label = quality_labels[source.pixiv_quality]
            raise PixivError(f"Pixiv 没有找到符合年龄段且提供{quality_label}画质的图片")
        return candidates

    async def _client(self, token: str) -> Any:
        if token in self._clients:
            return self._clients[token]
        try:
            from pixivpy3 import AppPixivAPI
        except ImportError as exc:
            raise PixivError("缺少 pixivpy3 依赖，请重新安装插件依赖") from exc
        client = AppPixivAPI()
        self._clients[token] = client
        self._locks[token] = asyncio.Lock()
        return client

    @staticmethod
    async def _search(client: Any, word: str) -> list[Any]:
        params: dict[str, Any] = {
            "word": word,
            "search_target": "exact_match_for_tags",
            "sort": "date_desc",
            "filter": "for_ios",
        }
        result_items: list[Any] = []
        for _ in range(3):
            result = await asyncio.to_thread(client.search_illust, **params)
            result_items.extend(_value(result, "illusts", []) or [])
            next_url = _value(result, "next_url", "")
            if not next_url:
                break
            params = client.parse_qs(next_url) or {}
        return result_items
