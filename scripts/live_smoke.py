from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from PIL import Image

from smart_collector.models import ContentType, SourceConfig
from smart_collector.pipeline import CollectorPipeline

SOURCES = (
    SourceConfig(
        key="live:yaohud",
        template="api",
        name="妖狐 API",
        enabled=True,
        url="https://api.yaohud.cn/api/v2/setu",
        headers={"key": "RgDEYLevGRcMSNIF8z9"},
        content_types=(ContentType.IMAGE,),
        command="/api",
        dedupe=-1,
        rate_limit=-1,
    ),
    SourceConfig(
        key="live:mukyu",
        template="website",
        name="Mukyu 随机插画",
        enabled=True,
        url="https://i.mukyu.ru/random?r18=1",
        content_types=(ContentType.IMAGE,),
        command="/mukyu",
        dedupe=-1,
        rate_limit=-1,
    ),
    SourceConfig(
        key="live:video",
        template="website",
        name="Avbebe H 动画影片",
        enabled=True,
        url="https://avbebe.com/archives/category/h%e5%8b%95%e7%95%ab%e5%bd%b1%e7%89%87",
        content_types=(ContentType.VIDEO,),
        command="/video",
        dedupe=-1,
        rate_limit=-1,
    ),
    SourceConfig(
        key="live:pektino",
        template="website",
        name="Pektino 随机分页视频",
        enabled=True,
        url="https://pektino.com/zh-CN/all",
        content_types=(ContentType.VIDEO,),
        command="/pektino",
        dedupe=-1,
        rate_limit=-1,
    ),
)


async def main(include_video: bool, include_pektino: bool) -> int:
    selected = list(SOURCES[:2])
    if include_video:
        selected.append(SOURCES[2])
    if include_pektino:
        selected.append(SOURCES[3])
    with tempfile.TemporaryDirectory(prefix="smart-collector-live-") as temp:
        pipeline = CollectorPipeline(Path(temp), concurrency=3, timeout=35)
        await pipeline.initialize()
        results = await asyncio.gather(
            *(pipeline.collect(source, None, "live-smoke") for source in selected),
            return_exceptions=True,
        )
        report = []
        failed = False
        for source, result in zip(selected, results, strict=True):
            if isinstance(result, BaseException):
                failed = True
                report.append({"source": source.name, "ok": False, "error": str(result)})
            else:
                verified = result.exists
                if verified and result.content_type is ContentType.IMAGE and result.local_path:
                    try:
                        with Image.open(result.local_path) as image:
                            image.verify()
                    except Exception:
                        verified = False
                if verified and result.content_type is ContentType.VIDEO and result.local_path:
                    verified = (
                        result.local_path.stat().st_size > 1024
                        and result.local_path.suffix.lower() not in {".m3u8", ".txt"}
                        and result.mime_type.startswith("video/")
                    )
                report.append(
                    {
                        "source": source.name,
                        "ok": verified,
                        "type": result.content_type.value,
                        "mime": result.mime_type,
                        "bytes": result.local_path.stat().st_size if result.local_path else 0,
                        "origin": result.origin_url,
                    }
                )
                failed = failed or not verified
        print(json.dumps(report, ensure_ascii=False, indent=2))
        await pipeline.close()
        return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-video", action="store_true", help="同时测试 Avbebe 视频网站")
    parser.add_argument(
        "--include-pektino", action="store_true", help="同时测试 Pektino 随机分页视频"
    )
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(arguments.include_video, arguments.include_pektino)))
