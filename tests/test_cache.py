import asyncio
from pathlib import Path

from smart_collector.cache import CacheStore
from smart_collector.models import CollectedAsset, ContentType


def test_cache_and_dedupe_semantics(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = CacheStore(tmp_path)
        await store.initialize()
        file_path = store.files_dir / "a.jpg"
        file_path.write_bytes(b"image")
        asset = CollectedAsset(
            asset_key="asset-a",
            source_key="source",
            source_name="Source",
            content_type=ContentType.IMAGE,
            origin_url="https://example.com/a.jpg",
            local_path=file_path,
        )
        await store.save_asset(asset)
        cached = await store.get_asset_by_origin("source", asset.origin_url)
        assert cached and cached.cached and cached.local_path == file_path
        allowed = await store.get_allowed_assets_by_origins(
            "source", [asset.origin_url, asset.origin_url], 0
        )
        assert list(allowed) == [asset.origin_url]
        assert allowed[asset.origin_url].cached
        assert await store.is_allowed("source", "asset-a", 0)
        await store.mark_sent("source", "asset-a")
        assert await store.get_allowed_assets_by_origins("source", [asset.origin_url], 0) == {}
        assert asset.origin_url in await store.get_allowed_assets_by_origins(
            "source", [asset.origin_url], -1
        )
        assert not await store.is_allowed("source", "asset-a", 0)
        assert not await store.is_allowed("source", "asset-a", 1)
        assert await store.is_allowed("source", "asset-a", -1)
        await store.close()

    asyncio.run(scenario())


def test_cleanup_preserves_files_referenced_by_another_source(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = CacheStore(tmp_path)
        await store.initialize()
        shared = store.files_dir / "shared.mp4"
        shared.write_bytes(b"video")
        for source_key in ("source-a", "source-b"):
            await store.save_asset(
                CollectedAsset(
                    asset_key=f"asset-{source_key}",
                    source_key=source_key,
                    source_name=source_key,
                    content_type=ContentType.VIDEO,
                    origin_url=f"https://example.com/{source_key}.mp4",
                    local_path=shared,
                )
            )
        assert await store.cleanup({"source-a": 0}) == 1
        assert shared.exists()
        assert await store.cleanup_all(0) == 1
        assert not shared.exists()
        await store.close()

    asyncio.run(scenario())


def test_configured_schedule_state_persists_last_slot(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = CacheStore(tmp_path)
        await store.initialize()
        initial = await store.schedule_state("source", "bot:GroupMessage:123")
        assert initial["last_slot"] == ""
        await store.mark_schedule_slot("source", "bot:GroupMessage:123", "2026-08-01T23:00")
        current = await store.schedule_state("source", "bot:GroupMessage:123")
        assert current["first_seen"] == initial["first_seen"]
        assert current["last_slot"] == "2026-08-01T23:00"
        await store.close()

    asyncio.run(scenario())
