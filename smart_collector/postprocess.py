from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from PIL import Image

from .models import CollectedAsset, ContentType


class PostProcessor:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def image_to_pdf(self, asset: CollectedAsset) -> CollectedAsset:
        if asset.content_type is not ContentType.IMAGE or not asset.local_path:
            return asset
        target_dir = self.output_dir / asset.asset_key.replace(":", "_")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "炸金~❤️.pdf"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            await asyncio.to_thread(self._image_to_pdf_sync, asset.local_path, temporary)
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return CollectedAsset(
            asset_key=asset.asset_key + ":pdf",
            source_key=asset.source_key,
            source_name=asset.source_name,
            content_type=ContentType.IMAGE,
            origin_url=asset.origin_url,
            title="炸金~❤️.pdf",
            mime_type="application/pdf",
            local_path=target,
            cached=asset.cached,
        )

    @staticmethod
    def _image_to_pdf_sync(source: Path, target: Path) -> None:
        with Image.open(source) as image:
            if image.mode in {"RGBA", "LA", "P"}:
                background = Image.new("RGB", image.size, "white")
                if image.mode == "P":
                    image = image.convert("RGBA")
                alpha = image.getchannel("A") if "A" in image.getbands() else None
                background.paste(image, mask=alpha)
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            image.save(target, "PDF", resolution=100.0)

    async def compress(self, asset: CollectedAsset, password: str = "") -> CollectedAsset:
        if not asset.local_path:
            return asset
        target = self.output_dir / f"{asset.asset_key.replace(':', '_')}.zip"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            await asyncio.to_thread(self._compress_sync, asset.local_path, temporary, password)
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return CollectedAsset(
            asset_key=asset.asset_key + ":zip",
            source_key=asset.source_key,
            source_name=asset.source_name,
            content_type=asset.content_type,
            origin_url=asset.origin_url,
            title=(asset.title or asset.local_path.name) + ".zip",
            mime_type="application/zip",
            local_path=target,
            cached=asset.cached,
        )

    @staticmethod
    def _compress_sync(source: Path, target: Path, password: str) -> None:
        if password:
            import pyzipper

            with pyzipper.AESZipFile(
                target,
                "w",
                compression=pyzipper.ZIP_DEFLATED,
                encryption=pyzipper.WZ_AES,
            ) as archive:
                archive.setpassword(password.encode("utf-8"))
                archive.write(source, arcname=source.name)
        else:
            import zipfile

            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(source, arcname=source.name)
