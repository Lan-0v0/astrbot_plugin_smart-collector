import asyncio
import re
from pathlib import Path

import pyzipper
from PIL import Image

from smart_collector.models import CollectedAsset, ContentType
from smart_collector.postprocess import PostProcessor


def test_pdf_and_password_zip(tmp_path: Path) -> None:
    async def scenario() -> None:
        image_path = tmp_path / "transparent.png"
        Image.new("RGBA", (32, 32), (255, 0, 0, 128)).save(image_path)
        asset = CollectedAsset(
            asset_key="image-key",
            source_key="source",
            source_name="Source",
            content_type=ContentType.IMAGE,
            origin_url="https://example.com/image.png",
            title="image.png",
            mime_type="image/png",
            local_path=image_path,
        )
        processor = PostProcessor(tmp_path / "output")
        pdf = await processor.image_to_pdf(asset)
        assert pdf.local_path and pdf.local_path.read_bytes().startswith(b"%PDF")
        assert pdf.local_path.name == "炸金~❤️.pdf"
        assert pdf.title == "炸金~❤️.pdf"
        archive = await processor.compress(asset, "secret")
        assert archive.local_path
        with pyzipper.AESZipFile(archive.local_path) as zipped:
            zipped.setpassword(b"secret")
            assert zipped.read("transparent.png") == image_path.read_bytes()

    asyncio.run(scenario())


def test_multi_image_pdf_and_zip_include_every_page(tmp_path: Path) -> None:
    async def scenario() -> None:
        first_path = tmp_path / "page.png"
        second_path = tmp_path / "second" / "page.png"
        second_path.parent.mkdir()
        Image.new("RGB", (32, 32), "red").save(first_path)
        Image.new("RGB", (32, 32), "blue").save(second_path)
        first = CollectedAsset(
            asset_key="first",
            source_key="source",
            source_name="Source",
            content_type=ContentType.IMAGE,
            origin_url="https://example.com/first.png",
            mime_type="image/png",
            local_path=first_path,
        )
        second = CollectedAsset(
            asset_key="second",
            source_key="source",
            source_name="Source",
            content_type=ContentType.IMAGE,
            origin_url="https://example.com/second.png",
            mime_type="image/png",
            local_path=second_path,
        )
        first.attachments = [second]
        first.history_keys = (first.asset_key, second.asset_key)
        processor = PostProcessor(tmp_path / "output")

        pdf = await processor.image_to_pdf(first)
        assert pdf.local_path
        assert len(re.findall(rb"/Type\s*/Page\b", pdf.local_path.read_bytes())) == 2
        assert pdf.history_keys == ("first", "second")

        archive = await processor.compress(first)
        assert archive.local_path
        with pyzipper.AESZipFile(archive.local_path) as zipped:
            assert sorted(zipped.namelist()) == ["page.png", "page_1.png"]
        assert archive.history_keys == ("first", "second")

    asyncio.run(scenario())


def test_postprocess_keeps_untrusted_asset_keys_inside_output_directory(tmp_path: Path) -> None:
    async def scenario() -> None:
        image_path = tmp_path / "image.png"
        Image.new("RGB", (16, 16), "red").save(image_path)
        asset = CollectedAsset(
            asset_key="../outside\\nested:asset",
            source_key="source",
            source_name="Source",
            content_type=ContentType.IMAGE,
            origin_url="https://example.com/image.png",
            local_path=image_path,
        )
        output_dir = tmp_path / "output"
        processor = PostProcessor(output_dir)
        pdf = await processor.image_to_pdf(asset)
        archive = await processor.compress(asset)
        assert pdf.local_path
        assert archive.local_path
        assert pdf.local_path.resolve().is_relative_to(output_dir.resolve())
        assert archive.local_path.resolve().is_relative_to(output_dir.resolve())

    asyncio.run(scenario())
