from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from PIL import Image

from .models import CollectedAsset, ContentType, safe_output_name


class PostProcessor:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def image_to_pdf(self, asset: CollectedAsset) -> CollectedAsset:
        if asset.content_type is not ContentType.IMAGE or not asset.local_path:
            return asset
        images = [item for item in (asset, *asset.attachments) if item.local_path]
        target_dir = self.output_dir / safe_output_name(asset.asset_key)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "炸金~❤️.pdf"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            await asyncio.to_thread(
                self._images_to_pdf_sync,
                [item.local_path for item in images if item.local_path],
                temporary,
            )
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
            history_keys=asset.history_keys or tuple(item.asset_key for item in images),
            r18=asset.r18,
        )

    @staticmethod
    def _pdf_page(source: Path) -> Image.Image:
        with Image.open(source) as image:
            if image.mode in {"RGBA", "LA", "P"}:
                background = Image.new("RGB", image.size, "white")
                if image.mode == "P":
                    image = image.convert("RGBA")
                alpha = image.getchannel("A") if "A" in image.getbands() else None
                background.paste(image, mask=alpha)
                return background
            if image.mode != "RGB":
                return image.convert("RGB")
            return image.copy()

    @classmethod
    def _images_to_pdf_sync(cls, sources: list[Path], target: Path) -> None:
        pages = [cls._pdf_page(source) for source in sources]
        if not pages:
            raise ValueError("没有可写入 PDF 的图片")
        try:
            pages[0].save(
                target,
                "PDF",
                resolution=100.0,
                save_all=True,
                append_images=pages[1:],
            )
        finally:
            for page in pages:
                page.close()

    async def compress(self, asset: CollectedAsset, password: str = "") -> CollectedAsset:
        if not asset.local_path:
            return asset
        assets = [item for item in (asset, *asset.attachments) if item.local_path]
        target = self.output_dir / f"{safe_output_name(asset.asset_key)}.zip"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            await asyncio.to_thread(
                self._compress_sync,
                [item.local_path for item in assets if item.local_path],
                temporary,
                password,
            )
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
            history_keys=asset.history_keys or tuple(item.asset_key for item in assets),
            r18=asset.r18,
        )

    @staticmethod
    def _compress_sync(sources: list[Path], target: Path, password: str) -> None:
        names: dict[str, int] = {}

        def archive_name(source: Path) -> str:
            count = names.get(source.name, 0)
            names[source.name] = count + 1
            return source.name if count == 0 else f"{source.stem}_{count}{source.suffix}"

        if password:
            import pyzipper

            with pyzipper.AESZipFile(
                target,
                "w",
                compression=pyzipper.ZIP_DEFLATED,
                encryption=pyzipper.WZ_AES,
            ) as archive:
                archive.setpassword(password.encode("utf-8"))
                for source in sources:
                    archive.write(source, arcname=archive_name(source))
        else:
            import zipfile

            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for source in sources:
                    archive.write(source, arcname=archive_name(source))
