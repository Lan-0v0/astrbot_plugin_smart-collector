import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parents[1]


class Decorators:
    class EventMessageType:
        ALL = "all"

    @staticmethod
    def _decorator(*args, **kwargs):
        def apply(function):
            return function

        return apply

    command = _decorator
    event_message_type = _decorator
    llm_tool = _decorator


def test_plugin_module_loads_with_official_api_surface(monkeypatch, tmp_path: Path) -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.api.message_components")

    class Star:
        def __init__(self, context, config=None):
            self.context = context

    class Component:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        @classmethod
        def fromFileSystem(cls, path):
            return cls(path)

    for name in ("Plain", "File", "Image", "Video", "Record", "Node"):
        setattr(components, name, type(name, (Component,), {}))
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.MessageChain = type("MessageChain", (Component,), {})
    event.MessageEventResult = type("MessageEventResult", (), {})
    event.filter = Decorators
    star.Context = type("Context", (), {})
    star.Star = Star
    star.StarTools = type("StarTools", (), {})
    star.register = Decorators._decorator

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.api.message_components": components,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    package = types.ModuleType("plugin_under_test")
    package.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "plugin_under_test", package)
    spec = importlib.util.spec_from_file_location("plugin_under_test.main", ROOT / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.SmartCollectorPlugin.__name__ == "SmartCollectorPlugin"
    plugin = module.SmartCollectorPlugin(None, {})
    source = plugin._temporary_source("https://example.com/page")
    assert source.content_types == module.CONTENT_PRIORITY
    assert source.forward_mode == "none"
    asset = module.CollectedAsset(
        asset_key="asset",
        source_key=source.key,
        source_name=source.name,
        content_type=module.ContentType.IMAGE,
        origin_url="https://cdn.example/image.jpg",
        mime_type="image/jpeg",
        local_path=ROOT / "image.jpg",
    )
    chain = plugin._build_chain(source, asset)
    assert len(chain) == 1
    assert type(chain[0]).__name__ == "Image"

    source.forward_mode = "user"
    forwarded = plugin._build_chain(source, asset, "10001", "用户")
    assert len(forwarded) == 1
    assert type(forwarded[0]).__name__ == "Node"
    assert len(forwarded[0].kwargs["content"]) == 1
    assert type(forwarded[0].kwargs["content"][0]).__name__ == "Image"

    class CommandEvent:
        message_str = "/爬取"

        @staticmethod
        def plain_result(value):
            return value

    async def command_scenario() -> None:
        results = [item async for item in plugin.collect_command(CommandEvent())]
        assert results == [module.COLLECT_COMMAND_USAGE]

    asyncio.run(command_scenario())

    class Platform:
        @staticmethod
        def meta():
            return types.SimpleNamespace(name="aiocqhttp", id="onebot-main")

    async def schedule_scenario() -> None:
        plugin.context = types.SimpleNamespace(
            platform_manager=types.SimpleNamespace(platform_insts=[Platform()])
        )
        plugin.pipeline = module.CollectorPipeline(tmp_path)
        await plugin.pipeline.initialize()
        scheduled = module.SourceConfig.from_mapping(
            {
                "name": "定时视频",
                "url": "https://example.com",
                "schedules": ["每天"],
                "target_qq_groups": ["123456", "654321"],
            }
        )
        targets = await plugin._scheduled_targets({scheduled.key: scheduled})
        assert {item["umo"] for item in targets} == {
            "onebot-main:GroupMessage:123456",
            "onebot-main:GroupMessage:654321",
        }
        assert all(item["configured_group"] for item in targets)
        await plugin.pipeline.close()

        class ScheduledCache:
            def __init__(self):
                self.slots = []

            async def mark_schedule_slot(self, source_key, umo, slot):
                self.slots.append((source_key, umo, slot))

            async def mark_slot(self, source_key, umo, slot):
                raise AssertionError("configured groups must use schedule state")

        class ScheduledPipeline:
            def __init__(self):
                self.cache = ScheduledCache()
                self.collect_calls = 0
                self.sent_assets = []

            async def collect(self, source, requested, user_key):
                self.collect_calls += 1
                return asset

            async def mark_sent(self, source, current_asset, cache_days):
                self.sent_assets.append(current_asset)

        sent_messages = []

        async def send_message(umo, chain):
            sent_messages.append((umo, chain))

        scheduled_pipeline = ScheduledPipeline()
        plugin.pipeline = scheduled_pipeline
        plugin.context.send_message = send_message
        due = [(target, "2026-08-01T23:00") for target in targets]
        await plugin._send_scheduled(scheduled, due)
        assert scheduled_pipeline.collect_calls == 1
        assert len(sent_messages) == 2
        assert len(scheduled_pipeline.sent_assets) == 1
        assert len(scheduled_pipeline.cache.slots) == 2

    asyncio.run(schedule_scenario())
