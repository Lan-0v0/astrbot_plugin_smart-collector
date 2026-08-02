import json
from pathlib import Path

import yaml

from smart_collector.config import load_sources, requested_types, split_url_request
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
        "image_ignore_size_kb",
        "concurrency",
        "request_timeout",
        "summary_provider",
        "summary_prompt",
        "cache_days",
    ]
    assert schema["image_ignore_size_kb"]["default"] == 100
    assert "slider" not in schema["image_ignore_size_kb"]
    assert list(schema).index("image_ignore_size_kb") + 1 == list(schema).index("concurrency")
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
    assert default["forward_mode"] == "none"
    for key in ("summary_provider", "summary_prompt", "cache_days"):
        assert key not in default
    templates = schema["custom_sources"]["templates"]
    assert set(templates) == {"website", "api", "pixiv"}
    website_items = templates["website"]["items"]
    assert website_items["video_url_only"]["default"] is False
    assert list(website_items).index("video_url_only") == list(website_items).index("compress") + 1
    assert "video_url_only" not in templates["api"]["items"]
    assert "video_url_only" not in templates["pixiv"]["items"]
    for template_key, template in templates.items():
        items = template["items"]
        assert items["dedupe"]["slider"] == {"min": -1, "max": 0, "step": 1}
        assert items["forward_mode"]["default"] == "user"
        assert items["target_qq_groups"]["default"] == []
        keys = list(items)
        assert keys.index("target_qq_groups") == keys.index("schedule_time") + 1
        if template_key != "pixiv":
            assert items["video_quality"]["default"] == "lowest"
            assert keys.index("video_quality") == keys.index("dedupe") + 1
        for key in ("summary_provider", "summary_prompt", "cache_days"):
            assert key not in items
    pixiv_items = templates["pixiv"]["items"]
    assert "/pixiv本地登陆" in templates["pixiv"]["hint"]
    assert "/pixiv远程登陆" in templates["pixiv"]["hint"]
    assert list(pixiv_items) == [
        "name",
        "enabled",
        "command",
        "age_mode",
        "quality",
        "dedupe",
        "image_to_pdf",
        "compress",
        "compression_password",
        "forward_mode",
        "custom_qq",
        "rate_limit",
        "schedules",
        "schedule_time",
        "target_qq_groups",
    ]
    assert pixiv_items["age_mode"]["default"] == "all"
    assert pixiv_items["quality"]["options"] == ["original", "large", "medium"]
    assert pixiv_items["quality"]["labels"] == ["原图", "大图", "中图"]
    assert pixiv_items["quality"]["default"] == "original"
    source = load_sources({"custom_sources": [default]})[0]
    assert source.headers == {"key": "RgDEYLevGRcMSNIF8z9"}
    assert source.content_types == (ContentType.IMAGE,)
    assert source.forward_mode == "none"


def test_metadata_version_and_required_fields() -> None:
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "astrbot_plugin_smart_collector"
    assert metadata["version"] == "v0.3.5"
    assert metadata["repo"] == "https://github.com/Lan-0v0/astrbot_plugin_smart-collector"


def test_natural_language_type_selection_and_priority() -> None:
    assert requested_types("来个图片", CONTENT_PRIORITY) == (ContentType.IMAGE,)
    assert requested_types("没有明确要求", CONTENT_PRIORITY) == CONTENT_PRIORITY
    assert normalize_types(["视频", "audio", "插画", "text"]) == CONTENT_PRIORITY


def test_url_command_request_parsing() -> None:
    assert split_url_request("图片 https://example.com/a?page=2") == (
        "https://example.com/a?page=2",
        "图片",
    )
    assert split_url_request("https://example.com/a。 视频") == (
        "https://example.com/a",
        "视频",
    )


def test_source_normalization() -> None:
    source = SourceConfig.from_mapping(
        {
            "__template_key": "website",
            "name": "示例",
            "url": "https://example.com",
            "content_types": ["image"],
            "command": "图片",
            "cookies": ["a=1", "b=2"],
            "video_quality": "highest",
            "video_url_only": True,
            "target_qq_groups": [123456, "123456", "bad", " 654321 "],
        }
    )
    assert source.command == "/图片"
    assert source.cookies == ("a=1", "b=2")
    assert source.video_quality == "highest"
    assert source.video_url_only
    assert source.target_qq_groups == ("123456", "654321")
    assert source.key.startswith("website:示例:")
    assert normalize_time("7:05") == "07:05"
    assert normalize_time("25:00") == "23:00"


def test_source_normalization_tolerates_invalid_legacy_values() -> None:
    source = SourceConfig.from_mapping(
        {
            "template": "unknown",
            "url": "https://example.com",
            "dedupe": "forever",
            "rate_limit": None,
            "forward_mode": "invalid",
            "schedules": "每天",
        }
    )
    assert source.template == "website"
    assert source.dedupe == 0
    assert source.rate_limit == 1.0
    assert source.forward_mode == "user"
    assert source.schedules == ("每天",)
    assert not source.video_url_only

    source = SourceConfig.from_mapping(
        {"url": "https://example.com", "dedupe": -99, "rate_limit": "-1"}
    )
    assert source.dedupe == -1
    assert source.rate_limit == -1.0

    api_source = SourceConfig.from_mapping(
        {"__template_key": "api", "url": "https://example.com/api", "video_url_only": True}
    )
    pixiv_source = SourceConfig.from_mapping({"__template_key": "pixiv", "video_url_only": True})
    assert not api_source.video_url_only
    assert not pixiv_source.video_url_only


def test_source_boolean_and_rate_values_are_normalized() -> None:
    source = SourceConfig.from_mapping(
        {
            "url": "https://example.com",
            "enabled": "false",
            "image_to_pdf": "0",
            "compress": "true",
            "pixiv_r18_to_pdf": "yes",
            "rate_limit": 99,
        }
    )
    assert not source.enabled
    assert not source.image_to_pdf
    assert source.compress
    assert source.pixiv_r18_to_pdf
    assert source.rate_limit == 1.0


def test_pixiv_source_normalization_without_url() -> None:
    sources = load_sources(
        {
            "custom_sources": [
                {
                    "__template_key": "pixiv",
                    "name": "P站",
                    "command": "p",
                    "age_mode": "r18",
                    "quality": "large",
                }
            ]
        }
    )
    assert len(sources) == 1
    source = sources[0]
    assert source.template == "pixiv"
    assert source.url == "https://app-api.pixiv.net/"
    assert source.command == "/p"
    assert source.content_types == (ContentType.IMAGE,)
    assert source.pixiv_age_mode == "r18"
    assert source.pixiv_quality == "large"

    legacy_source = SourceConfig.from_mapping(
        {"__template_key": "pixiv", "name": "旧配置", "quality": "invalid"}
    )
    assert legacy_source.pixiv_quality == "original"
