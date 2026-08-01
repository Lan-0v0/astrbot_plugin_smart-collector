import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from smart_collector.models import ContentType, SourceConfig
from smart_collector.pixiv import PixivAuthManager, PixivCollector, PixivError, parse_pixiv_query


def test_parse_multiple_tags_and_natural_language_age() -> None:
    assert parse_pixiv_query("百合 JK 白丝") == ("百合 JK 白丝", "all")
    assert parse_pixiv_query("帮我在p站上找R18的百合图") == ("百合", "r18")
    assert parse_pixiv_query("全年龄 风景", "r18") == ("风景", "safe")
    with pytest.raises(PixivError, match="至少一个 Tag"):
        parse_pixiv_query("R18")


def test_pixiv_collector_extracts_original_pages_and_filters_age(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.auth_calls = 0
            self.search_calls = 0

        def auth(self, *, refresh_token: str) -> None:
            assert refresh_token == "test-token"
            self.auth_calls += 1

        def search_illust(self, **params):
            self.search_calls += 1
            assert params["word"] == "百合 JK"
            assert params["search_target"] == "exact_match_for_tags"
            return {
                "illusts": [
                    {
                        "title": "全年龄",
                        "x_restrict": 0,
                        "width": 1200,
                        "height": 1800,
                        "meta_single_page": {
                            "original_image_url": "https://i.pximg.net/img-original/safe.jpg"
                        },
                        "meta_pages": [],
                        "tags": [{"name": "百合"}],
                    },
                    {
                        "title": "R18 多图",
                        "x_restrict": 1,
                        "width": 1600,
                        "height": 2400,
                        "meta_single_page": {},
                        "meta_pages": [
                            {
                                "image_urls": {
                                    "original": "https://i.pximg.net/img-original/r18-p0.jpg"
                                }
                            },
                            {
                                "image_urls": {
                                    "original": "https://i.pximg.net/img-original/r18-p1.jpg"
                                }
                            },
                        ],
                        "tags": [{"name": "百合"}, {"name": "JK"}],
                    },
                ],
                "next_url": "",
            }

    async def scenario() -> None:
        source = SourceConfig.from_mapping(
            {
                "__template_key": "pixiv",
                "name": "P站",
                "refresh_token": "test-token",
                "age_mode": "r18",
            }
        )
        collector = PixivCollector(tmp_path)
        client = FakeClient()
        collector._clients["test-token"] = client
        collector._locks["test-token"] = asyncio.Lock()
        candidates = await collector.candidates(source, "百合 JK")
        assert client.auth_calls == 1
        assert client.search_calls == 1
        assert [item.url for item in candidates] == [
            "https://i.pximg.net/img-original/r18-p0.jpg",
            "https://i.pximg.net/img-original/r18-p1.jpg",
        ]
        assert all(item.content_type is ContentType.IMAGE for item in candidates)
        assert all(item.referer == "https://www.pixiv.net/" for item in candidates)

    asyncio.run(scenario())


def test_pixiv_requires_login(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = SourceConfig.from_mapping({"__template_key": "pixiv", "name": "P站"})
        with pytest.raises(PixivError, match="未配置 Refresh Token"):
            await PixivCollector(tmp_path).candidates(source, "百合")

    asyncio.run(scenario())


def test_pixiv_oauth_qr_and_callback_persist_token(monkeypatch, tmp_path: Path) -> None:
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"refresh_token": "saved-refresh-token"}

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            assert kwargs["follow_redirects"] is True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, data):
            assert url.endswith("/auth/token")
            assert data["code"] == "oauth-code"
            assert data["code_verifier"]
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    async def scenario() -> None:
        auth = PixivAuthManager(tmp_path)
        login_url = await auth.start()
        params = parse_qs(urlsplit(login_url).query)
        assert params["code_challenge_method"] == ["S256"]
        assert params["client"] == ["pixiv-android"]
        state = params["state"][0]
        await auth.finish(f"pixiv://account/login?code=oauth-code&state={state}")
        assert await auth.load_refresh_token() == "saved-refresh-token"
        saved = json.loads((tmp_path / "pixiv_auth.json").read_text(encoding="utf-8"))
        assert saved["refresh_token"] == "saved-refresh-token"

    asyncio.run(scenario())


def test_pixiv_oauth_rejects_callback_without_pending_login(tmp_path: Path) -> None:
    async def scenario() -> None:
        with pytest.raises(PixivError, match="二维码已过期"):
            await PixivAuthManager(tmp_path).finish("pixiv://account/login?code=value")

    asyncio.run(scenario())
