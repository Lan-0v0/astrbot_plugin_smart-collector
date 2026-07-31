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
        name="Twitter Ero Video Ranking",
        enabled=True,
        url="https://twitter-ero-video-ranking.com",
        content_types=(ContentType.VIDEO,),
        command="/video",
        dedupe=-1,
        rate_limit=-1,
    ),
)


async def main(include_video: bool) -> int:
    selected = SOURCES if include_video else SOURCES[:2]
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
    parser.add_argument("--include-video", action="store_true", help="同时测试视频排行网站")
    raise SystemExit(asyncio.run(main(parser.parse_args().include_video)))
