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
    custom_filter = _decorator
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

        @classmethod
        def fromBytes(cls, value):
            return cls(value)

    class CustomFilter:
        def __init__(self, *args, **kwargs):
            pass

    for name in ("Plain", "File", "Image", "Video", "Record", "Node"):
        setattr(components, name, type(name, (Component,), {}))
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("test")
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.MessageChain = type("MessageChain", (Component,), {})
    event.MessageEventResult = type("MessageEventResult", (), {})
    event.filter = Decorators
    Decorators.CustomFilter = CustomFilter
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

    pdf_asset = module.CollectedAsset(
        asset_key="asset:pdf",
        source_key=source.key,
        source_name=source.name,
        content_type=module.ContentType.IMAGE,
        origin_url=asset.origin_url,
        title="random.pdf",
        mime_type="application/pdf",
        local_path=ROOT / "random.pdf",
    )
    pdf_chain = plugin._build_chain(source, pdf_asset)
    assert pdf_chain[0].kwargs["name"] == "炸金~❤️.pdf"

    source.forward_mode = "user"
    forwarded = plugin._build_chain(source, asset, "10001", "用户")
    assert len(forwarded) == 1
    assert type(forwarded[0]).__name__ == "Node"
    assert len(forwarded[0].kwargs["content"]) == 1
    assert type(forwarded[0].kwargs["content"][0]).__name__ == "Image"

    forwarded_pdf = plugin._build_chain(source, pdf_asset, "10001", "用户")
    assert len(forwarded_pdf) == 1
    assert type(forwarded_pdf[0]).__name__ == "File"
    assert forwarded_pdf[0].kwargs["file"] == str(ROOT / "random.pdf")

    second_image = module.CollectedAsset(
        asset_key="asset-2",
        source_key=source.key,
        source_name=source.name,
        content_type=module.ContentType.IMAGE,
        origin_url="https://cdn.example/image-2.jpg",
        mime_type="image/jpeg",
        local_path=ROOT / "image-2.jpg",
    )
    asset.attachments = [second_image]
    forwarded_album = plugin._build_chain(source, asset, "10001", "用户")
    assert type(forwarded_album[0]).__name__ == "Node"
    assert len(forwarded_album[0].kwargs["content"]) == 2
    asset.attachments = []

    class CommandEvent:
        message_str = "/爬取"

        @staticmethod
        def plain_result(value):
            return value

    async def command_scenario() -> None:
        results = [item async for item in plugin.collect_command(CommandEvent())]
        assert results == [module.COLLECT_COMMAND_USAGE]

    asyncio.run(command_scenario())

    class LocalAuth:
        called = False
        callback = ""

        async def login_local(self):
            self.called = True

        async def start(self):
            return "https://example.com/pixiv-login"

        async def finish(self, callback):
            self.callback = callback

    local_auth = LocalAuth()
    plugin.pipeline = types.SimpleNamespace(pixiv=types.SimpleNamespace(auth=local_auth))

    class LocalLoginEvent:
        message_str = "/pixiv本地登陆"

        @staticmethod
        def plain_result(value):
            return value

    async def local_login_scenario() -> None:
        results = [item async for item in plugin.pixiv_local_login_command(LocalLoginEvent())]
        assert local_auth.called
        assert results[0].startswith("已打开本地 Pixiv 登录窗口")
        assert results[1] == "Pixiv 登录成功，Refresh Token 已保存。"

    asyncio.run(local_login_scenario())

    class RemoteLoginEvent:
        message_str = "/pixiv远程登陆"

        @staticmethod
        def plain_result(value):
            return value

        @staticmethod
        def chain_result(value):
            return value

    class RemoteCallbackEvent(RemoteLoginEvent):
        message_str = "/pixiv远程登陆 pixiv://account/login?code=test"

    async def remote_login_scenario() -> None:
        results = [item async for item in plugin.pixiv_remote_login_command(RemoteLoginEvent())]
        assert not isinstance(results[0], str), results
        assert type(results[0][0]).__name__ == "Image"
        assert "登录链接：https://example.com/pixiv-login" in results[0][1].args[0]
        callback_results = [
            item async for item in plugin.pixiv_remote_login_command(RemoteCallbackEvent())
        ]
        assert local_auth.callback == "pixiv://account/login?code=test"
        assert callback_results == ["Pixiv 登录成功，Refresh Token 已保存。"]

    asyncio.run(remote_login_scenario())

    builtin = plugin._default_pixiv_source()
    assert builtin.key == "pixiv:__builtin__"
    assert builtin.command == "/pixiv"
    assert builtin.pixiv_age_mode == "all"
    assert builtin.pixiv_r18_to_pdf
    assert builtin.dedupe == 0
    assert not builtin.compress
    assert builtin.forward_mode == "none"
    assert builtin.rate_limit == 1.0
    assert builtin.schedules == ()

    configured_pixiv = module.SourceConfig.from_mapping(
        {
            "__template_key": "pixiv",
            "name": "自定义",
            "command": "/p",
            "age_mode": "safe",
            "dedupe": -1,
            "image_to_pdf": True,
            "compress": True,
            "forward_mode": "user",
            "rate_limit": -1,
        }
    )
    plugin.sources = [configured_pixiv]
    assert plugin._default_pixiv_source() == builtin
    assert "Pixiv（P站）图像采集专属帮助" in module.PIXIV_HELP
    assert "/pixiv本地登陆" in module.PIXIV_HELP
    assert "/pixiv远程登陆 [URL]" in module.PIXIV_HELP

    captured_sources = []

    async def capture_pixiv_sources(self, event, sources, query, explicit_types=None):
        captured_sources.extend(sources)
        yield query

    class PixivCommandEvent:
        message_str = "/pixiv 百合 jk r18"

    async def builtin_pixiv_command_scenario() -> None:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                module.SmartCollectorPlugin,
                "_collect_and_reply",
                capture_pixiv_sources,
            )
            results = [item async for item in plugin.pixiv_command(PixivCommandEvent())]
        assert results == ["百合 jk r18"]

    asyncio.run(builtin_pixiv_command_scenario())
    assert captured_sources == [builtin]
    conflicting_pixiv = module.SourceConfig.from_mapping(
        {"__template_key": "pixiv", "name": "冲突 Pixiv", "command": "/pixiv"}
    )
    assert module._match_custom_source("/pixiv 百合", [conflicting_pixiv]) == (None, "")

    class ReplyPipeline:
        def __init__(self):
            self.marked = []

        async def collect_many(self, sources, wanted, user_key, query):
            return sources[0], asset

        async def mark_sent(self, marked_source, marked_asset, cache_days):
            self.marked.append((marked_source, marked_asset))

    class ReplyEvent:
        unified_msg_origin = "platform:Message:user"

        @staticmethod
        def get_sender_id():
            return "10001"

        @staticmethod
        def get_sender_name():
            return "用户"

        @staticmethod
        def chain_result(chain):
            return chain

        @staticmethod
        def plain_result(value):
            return value

    async def first_result_marks_history_before_generator_resumes() -> None:
        reply_pipeline = ReplyPipeline()
        plugin.pipeline = reply_pipeline
        source.forward_mode = "none"
        generator = plugin._collect_and_reply(ReplyEvent(), [source], "")
        await generator.__anext__()
        assert reply_pipeline.marked == [(source, asset)]
        await generator.aclose()

    asyncio.run(first_result_marks_history_before_generator_resumes())

    movie_source = module.SourceConfig.from_mapping(
        {
            "name": "影片",
            "url": "https://example.com/movie",
            "command": "/看片",
        }
    )
    assert module._match_custom_source("看片", [movie_source]) == (movie_source, "")
    assert module._match_custom_source("/看片 视频", [movie_source]) == (
        movie_source,
        "视频",
    )
    assert module._match_custom_source("看片段", [movie_source]) == (None, "")
    pixiv_source = module.SourceConfig.from_mapping(
        {"__template_key": "pixiv", "name": "P站", "command": "/p"}
    )
    assert module._match_custom_source("/p 百合 JK 白丝", [pixiv_source]) == (
        pixiv_source,
        "百合 JK 白丝",
    )
    reserved_source = module.SourceConfig.from_mapping(
        {"name": "冲突项", "url": "https://example.com", "command": "/爬取"}
    )
    assert module._match_custom_source("爬取 https://example.com", [reserved_source]) == (
        None,
        "",
    )

    module.CUSTOM_SOURCE_COMMANDS.clear()
    module.CUSTOM_SOURCE_COMMANDS.update(module._command_variants(movie_source.command))
    source_filter = module.CustomSourceCommandFilter()
    stripped_event = types.SimpleNamespace(get_message_str=lambda: "看片")
    unrelated_event = types.SimpleNamespace(get_message_str=lambda: "普通聊天")
    assert source_filter.filter(stripped_event, {})
    assert not source_filter.filter(unrelated_event, {})

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

        async def failed_send_message(umo, chain):
            return False

        failed_pipeline = ScheduledPipeline()
        plugin.pipeline = failed_pipeline
        plugin.context.send_message = failed_send_message
        await plugin._send_scheduled(scheduled, due)
        assert failed_pipeline.cache.slots == []
        assert failed_pipeline.sent_assets == []

    asyncio.run(schedule_scenario())
