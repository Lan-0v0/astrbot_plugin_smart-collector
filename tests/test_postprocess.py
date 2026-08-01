import asyncio
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
