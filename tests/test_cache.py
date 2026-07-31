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
        assert await store.is_allowed("source", "asset-a", 0)
        await store.mark_sent("source", "asset-a")
        assert not await store.is_allowed("source", "asset-a", 0)
        assert not await store.is_allowed("source", "asset-a", 1)
        assert await store.is_allowed("source", "asset-a", -1)
        await store.close()

    asyncio.run(scenario())
