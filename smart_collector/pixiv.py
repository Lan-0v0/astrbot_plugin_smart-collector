from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import shlex
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
PIXIV_TOKEN_FILE = "pixiv_auth.json"


@dataclass(slots=True)
class PendingPixivLogin:
    verifier: str
    state: str
    created_at: float


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _pixiv_image_urls(illust: Any) -> list[tuple[str, int, int]]:
    urls: list[tuple[str, int, int]] = []
    single = _value(illust, "meta_single_page", {}) or {}
    original = _value(single, "original_image_url", "")
    if original:
        urls.append(
            (
                str(original),
                int(_value(illust, "width", 0) or 0),
                int(_value(illust, "height", 0) or 0),
            )
        )
    for page in _value(illust, "meta_pages", []) or []:
        image_urls = _value(page, "image_urls", {}) or {}
        page_url = _value(image_urls, "original", "") or _value(image_urls, "large", "")
        if page_url:
            urls.append(
                (
                    str(page_url),
                    int(_value(illust, "width", 0) or 0),
                    int(_value(illust, "height", 0) or 0),
                )
            )
    return list(dict.fromkeys(urls))


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
            raise PixivError("Pixiv 登录二维码已过期，请重新发送 /pixiv登陆")
        value = str(callback or "").strip()
        parts = urlsplit(value)
        params = parse_qs(parts.query) if parts.scheme else {"code": [value]}
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]
        if not code:
            error = (params.get("error_description") or params.get("error") or [""])[0]
            raise PixivError(f"Pixiv 登录失败：{error or '未找到授权 code'}")
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
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
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
            raise PixivError("Pixiv 未配置 Refresh Token，请先使用 /pixiv登陆")
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
            for url, width, height in _pixiv_image_urls(illust):
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
                    )
                )
        if not candidates:
            raise PixivError("Pixiv 没有找到符合年龄段的图片")
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
