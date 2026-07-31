import json
from pathlib import Path

import yaml

from smart_collector.config import load_sources, requested_types
from smart_collector.models import (
    CONTENT_PRIORITY,
    ContentType,
    SourceConfig,
    normalize_time,
    normalize_types,
)

ROOT = Path(__file__).parents[1]


def test_schema_contains_required_default_api() -> None:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert list(schema) == [
        "natural_language_enabled",
        "custom_sources",
        "concurrency",
        "request_timeout",
        "summary_provider",
        "summary_prompt",
        "cache_days",
    ]
    assert schema["concurrency"]["default"] == -1
    assert "slider" not in schema["concurrency"]
    assert schema["request_timeout"]["default"] == -1
    assert "slider" not in schema["request_timeout"]
    default = schema["custom_sources"]["default"][0]
    assert default["__template_key"] == "api"
    assert default["name"] == "Lanの默认配置"
    assert default["url"] == "https://api.yaohud.cn/api/v2/setu"
    assert default["header_key"] == "key"
    assert default["header_value"] == "RgDEYLevGRcMSNIF8z9"
    assert default["content_types"] == ["image"]
    assert default["command"] == "/插画"
    for key in ("summary_provider", "summary_prompt", "cache_days"):
        assert key not in default
    for template in schema["custom_sources"]["templates"].values():
        items = template["items"]
        assert items["dedupe"]["slider"] == {"min": -1, "max": 0, "step": 1}
        for key in ("summary_provider", "summary_prompt", "cache_days"):
            assert key not in items
    source = load_sources({"custom_sources": [default]})[0]
    assert source.headers == {"key": "RgDEYLevGRcMSNIF8z9"}
    assert source.content_types == (ContentType.IMAGE,)


def test_metadata_version_and_required_fields() -> None:
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "astrbot_plugin_smart_collector"
    assert metadata["version"] == "v0.0.2"
    assert metadata["repo"] == "https://github.com/Lan-0v0/astrbot_plugin_smart-collector"


def test_natural_language_type_selection_and_priority() -> None:
    assert requested_types("来个图片", CONTENT_PRIORITY) == (ContentType.IMAGE,)
    assert requested_types("没有明确要求", CONTENT_PRIORITY) == CONTENT_PRIORITY
    assert normalize_types(["视频", "audio", "插画", "text"]) == CONTENT_PRIORITY


def test_source_normalization() -> None:
    source = SourceConfig.from_mapping(
        {
            "__template_key": "website",
            "name": "示例",
            "url": "https://example.com",
            "content_types": ["image"],
            "command": "图片",
            "cookies": ["a=1", "b=2"],
        }
    )
    assert source.command == "/图片"
    assert source.cookies == ("a=1", "b=2")
    assert source.key.startswith("website:示例:")
    assert normalize_time("7:05") == "07:05"
    assert normalize_time("25:00") == "23:00"
