from __future__ import annotations

import asyncio
import io
import math
import re
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools, register

from .smart_collector.config import (
    DEFAULT_SUMMARY_PROMPT,
    load_sources,
    requested_types,
    split_url_request,
)
from .smart_collector.models import (
    CONTENT_PRIORITY,
    CollectedAsset,
    ContentType,
    SourceConfig,
    normalize_bool,
    normalize_types,
)
from .smart_collector.pipeline import CollectionError, CollectorPipeline
from .smart_collector.schedule import schedule_slot

COLLECT_COMMAND_USAGE = (
    "爬虫指令规范为：/爬取 [URL] [类型]\n其中URL为必须项，类型（视频/图片/音频/文字）为不必须项"
)
CUSTOM_SOURCE_COMMANDS: set[str] = set()
PIXIV_HELP = (
    "Pixiv（P站）图像采集专属帮助\n\n"
    "根据单/多tag随机爬图：\n"
    "/pixiv [Tag1] [Tag2] ... r18\n"
    "例如/pixiv 百合 jk r18，加上r18后会限制年龄段类型，"
    "不加则默认包含全年龄＋R18\n"
    "tips：可在配置面板中新建独特的专属指令\n\n"
    "登陆\n"
    "在bot部署的本地端上跳转网站登陆：\n"
    "/pixiv本地登陆\n"
    "远程登陆获取二维码/链接：\n"
    "/pixiv远程登陆\n"
    "/pixiv远程登陆 [URL] ——用指令回报code完成登陆"
)


def _config_number(value, default: int | float, converter):
    try:
        converted = converter(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if isinstance(converted, float) and not math.isfinite(converted):
        return default
    return converted


def _command_variants(command: str) -> tuple[str, ...]:
    value = command.strip()
    if not value:
        return ()
    without_slash = value[1:] if value.startswith("/") else value
    return tuple(dict.fromkeys(item for item in (value, without_slash) if item))


BUILTIN_COMMANDS = (
    "/爬取",
    "/抓取",
    "/pixiv",
    "/pixiv本地登陆",
    "/pixiv远程登陆",
)
RESERVED_COMMANDS = {
    variant for command in BUILTIN_COMMANDS for variant in _command_variants(command)
}


def _command_arguments(message: str, command: str) -> str | None:
    """Return command arguments when the message contains this exact command."""
    text = str(message or "").strip()
    if not text:
        return None
    for command_variant in _command_variants(command):
        match = re.match(
            rf"^{re.escape(command_variant)}(?:\s+(?P<arguments>.*))?$",
            text,
            flags=re.DOTALL,
        )
        if match:
            return (match.group("arguments") or "").strip()
    return None


def _qr_png(value: str) -> bytes:
    import qrcode

    output = io.BytesIO()
    qrcode.make(value).save(output, format="PNG")
    return output.getvalue()


def _match_custom_source(
    message: str, sources: list[SourceConfig]
) -> tuple[SourceConfig | None, str]:
    for source in sources:
        if not source.enabled:
            continue
        command_variants = _command_variants(source.command)
        if any(command in RESERVED_COMMANDS for command in command_variants):
            continue
        arguments = _command_arguments(message, source.command)
        if arguments is not None:
            return source, arguments
    return None, ""


class CustomSourceCommandFilter(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        message = event.get_message_str().strip()
        return any(
            _command_arguments(message, command) is not None for command in CUSTOM_SOURCE_COMMANDS
        )


@register(
    "astrbot_plugin_smart_collector",
    "Lan-0v0",
    "支持视频、音频、图片和文字的并发自适应采集插件",
    "v0.3.4",
)
class SmartCollectorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self.sources = load_sources(config)
        CUSTOM_SOURCE_COMMANDS.clear()
        for source in self.sources:
            if source.enabled:
                CUSTOM_SOURCE_COMMANDS.update(
                    command
                    for command in _command_variants(source.command)
                    if command not in RESERVED_COMMANDS
                )
        self.summary_provider = str(config.get("summary_provider") or "")
        self.summary_prompt = str(config.get("summary_prompt") or DEFAULT_SUMMARY_PROMPT)
        self.cache_days = int(_config_number(config.get("cache_days", 7), 7, int))
        self.cache_days = -1 if self.cache_days < 0 else min(7, self.cache_days)
        self.pipeline: CollectorPipeline | None = None
        self._tasks: list[asyncio.Task] = []

    async def initialize(self) -> None:
        data_dir = StarTools.get_data_dir("astrbot_plugin_smart_collector")
        image_ignore_size_kb = int(
            _config_number(self.config.get("image_ignore_size_kb", 100), 100, int)
        )
        image_ignore_size_kb = -1 if image_ignore_size_kb < 0 else image_ignore_size_kb
        concurrency = int(_config_number(self.config.get("concurrency", -1), -1, int))
        concurrency = -1 if concurrency < 0 else concurrency
        timeout = float(_config_number(self.config.get("request_timeout", -1), -1, float))
        timeout = -1.0 if timeout < 0 else max(0.1, timeout)
        self.pipeline = CollectorPipeline(
            data_dir,
            image_ignore_size_kb=image_ignore_size_kb,
            concurrency=concurrency,
            timeout=timeout,
        )
        await self.pipeline.initialize()
        if not normalize_bool(self.config.get("natural_language_enabled", True), True):
            with suppress(Exception):
                deactivate = getattr(self.context, "deactivate_llm_tool", None)
                if callable(deactivate):
                    deactivate("smart_collect")
                else:
                    self.context.unregister_llm_tool("smart_collect")
        self._tasks = [
            asyncio.create_task(self._scheduler_loop(), name="smart-collector-scheduler"),
            asyncio.create_task(self._cleanup_loop(), name="smart-collector-cleanup"),
        ]
        logger.info("Smart Collector v0.3.4 已加载，共 %d 个自定义爬取项", len(self.sources))

    @filter.command("pixiv")
    async def pixiv_command(self, event: AstrMessageEvent) -> MessageEventResult:
        query = _command_arguments(event.message_str, "/pixiv") or ""
        if not query:
            yield event.plain_result(PIXIV_HELP)
            return
        async for result in self._collect_and_reply(
            event, [self._default_pixiv_source()], query, (ContentType.IMAGE,)
        ):
            yield result

    @filter.command("pixiv本地登陆")
    async def pixiv_local_login_command(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self.pipeline:
            yield event.plain_result("Smart Collector 尚未初始化完成。")
            return
        try:
            yield event.plain_result(
                "已打开本地 Pixiv 登录窗口，请在十分钟内完成登录，插件将自动获取授权。"
            )
            await self.pipeline.pixiv.auth.login_local()
            yield event.plain_result("Pixiv 登录成功，Refresh Token 已保存。")
        except Exception as exc:
            yield event.plain_result(f"Pixiv 登录失败：{exc}")

    @filter.command("pixiv远程登陆")
    async def pixiv_remote_login_command(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self.pipeline:
            yield event.plain_result("Smart Collector 尚未初始化完成。")
            return
        callback = _command_arguments(event.message_str, "/pixiv远程登陆") or ""
        try:
            if callback:
                await self.pipeline.pixiv.auth.finish(callback)
                yield event.plain_result("Pixiv 登录成功，Refresh Token 已保存。")
                return
            login_url = await self.pipeline.pixiv.auth.start()
            yield event.chain_result(
                [
                    Comp.Image.fromBytes(_qr_png(login_url)),
                    Comp.Plain(
                        f"登录链接：{login_url}\n"
                        "在登陆后使用“/pixiv远程登陆 [URL]”以登陆\n"
                        "请复制最终包含 code 的 pixiv://account/login 回调地址；"
                        "accounts.pixiv.net/post-redirect 中间地址不能用于登录。"
                    ),
                ]
            )
        except Exception as exc:
            yield event.plain_result(f"Pixiv 登录失败：{exc}")

    @filter.command("爬取", alias={"抓取"})
    async def collect_command(self, event: AstrMessageEvent) -> MessageEventResult:
        """爬取指定 URL；可指定视频、图片、音频或文字。"""
        query = _command_arguments(event.message_str, "/爬取")
        if query is None:
            query = _command_arguments(event.message_str, "/抓取") or ""
        url, query = split_url_request(query)
        if not url:
            yield event.plain_result(COLLECT_COMMAND_USAGE)
            return
        async for result in self._collect_and_reply(event, [self._temporary_source(url)], query):
            yield result

    @filter.custom_filter(CustomSourceCommandFilter, priority=10)
    async def custom_source_commands(self, event: AstrMessageEvent) -> MessageEventResult:
        """识别配置中每个自定义爬取项的专属指令。"""
        source, query = _match_custom_source(event.get_message_str(), self.sources)
        if not source:
            return
        event.stop_event()
        async for result in self._collect_and_reply(event, [source], query):
            yield result

    @filter.llm_tool(name="smart_collect")
    async def smart_collect(
        self,
        event: AstrMessageEvent,
        query: str,
        source_name: str = "",
        content_types: list[str] | None = None,
        url: str = "",
    ) -> MessageEventResult:
        """按用户的自然语言要求，从配置的数据源采集视频、音频、图片或文字。

        Args:
            query(string): 用户对采集内容的自然语言要求
            source_name(string): 可选的数据源名称；留空时并发尝试所有已启用数据源
            content_types(array[string]): 可选内容类型，只能使用 video、audio、image、text
            url(string): 可选的临时 HTTP/HTTPS 抓取地址；提供后不使用已配置数据源
        """
        if not normalize_bool(self.config.get("natural_language_enabled", True), True):
            yield event.plain_result("自然语言爬取已在插件配置中关闭。")
            return
        query_url, query = split_url_request(query)
        target_url = url.strip() or query_url
        explicit_types: tuple[ContentType, ...] | None = None
        if not url.strip() and (
            source_name.strip().casefold() == "pixiv"
            or (not source_name.strip() and re.search(r"pixiv|p站", query, re.IGNORECASE))
        ):
            sources = self._pixiv_sources()
            explicit_types = (ContentType.IMAGE,)
        else:
            sources = (
                [self._temporary_source(target_url)]
                if target_url
                else self._match_sources(source_name)
            )
        explicit_types = normalize_types(content_types) if content_types else explicit_types
        async for result in self._collect_and_reply(event, sources, query, explicit_types):
            yield result

    async def terminate(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        if self.pipeline:
            await self.pipeline.close()

    async def _collect_and_reply(
        self,
        event: AstrMessageEvent,
        sources: list[SourceConfig],
        query: str,
        explicit_types: tuple[ContentType, ...] | None = None,
    ):
        if not self.pipeline:
            yield event.plain_result("Smart Collector 尚未初始化完成。")
            return
        if not sources:
            yield event.plain_result("没有找到符合名称的已启用爬取项。")
            return
        allowed_union = tuple(
            content_type
            for content_type in CONTENT_PRIORITY
            if any(content_type in source.content_types for source in sources)
        )
        wanted = explicit_types or requested_types(query, allowed_union)
        try:
            source, asset = await self.pipeline.collect_many(
                sources,
                wanted,
                user_key=event.get_sender_id() or event.unified_msg_origin,
                query=query,
            )
            await self._summarize(source, asset)
            result = event.chain_result(
                self._build_chain(source, asset, event.get_sender_id(), event.get_sender_name())
            )
            if source.schedules:
                await self.pipeline.cache.subscribe(
                    source.key,
                    event.unified_msg_origin,
                    event.get_sender_id(),
                    event.get_sender_name(),
                )
            try:
                await self.pipeline.mark_sent(source, asset, self.cache_days)
            except Exception:
                logger.exception("数据源 %s 的发送历史记录失败", source.name)
            yield result
        except CollectionError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.exception("Smart Collector 处理请求失败")
            yield event.plain_result(f"采集失败：{exc}")

    async def _summarize(self, source: SourceConfig, asset: CollectedAsset) -> None:
        if (
            asset.content_type is not ContentType.TEXT
            or not self.summary_provider
            or not asset.text
        ):
            return
        try:
            response = await self.context.llm_generate(
                chat_provider_id=self.summary_provider,
                prompt=f"{self.summary_prompt}\n\n<原文>\n{asset.text[:100_000]}\n</原文>",
            )
            asset.summary = response.completion_text.strip()
        except Exception:
            logger.exception("数据源 %s 的文字摘要生成失败，将发送原文", source.name)

    def _build_chain(
        self,
        source: SourceConfig,
        asset: CollectedAsset,
        sender_id: str = "",
        sender_name: str = "",
    ) -> list:
        content: list = []
        assets = (asset, *asset.attachments)
        contains_non_forwardable_component = False
        for item in assets:
            path = str(item.local_path) if item.local_path else ""
            if item.mime_type in {"application/pdf", "application/zip"}:
                file_name = "炸金~❤️.pdf" if item.mime_type == "application/pdf" else item.title
                content.append(Comp.File(name=file_name or Path(path).name, file=path))
                contains_non_forwardable_component = True
            elif item.content_type is ContentType.IMAGE:
                content.append(Comp.Image.fromFileSystem(path))
            elif item.content_type is ContentType.VIDEO:
                content.append(Comp.Video.fromFileSystem(path))
                contains_non_forwardable_component = True
            elif item.content_type is ContentType.AUDIO:
                content.append(Comp.Record.fromFileSystem(path))
                contains_non_forwardable_component = True
            else:
                text = item.summary or item.text
                content.append(Comp.Plain(text[:20_000]))
        if source.forward_mode == "none" or contains_non_forwardable_component:
            return content
        uin = source.custom_qq if source.forward_mode == "custom" else sender_id
        name = source.name if source.forward_mode == "custom" else (sender_name or source.name)
        if not uin:
            return content
        return [Comp.Node(uin=str(uin), name=name, content=content)]

    async def _scheduler_loop(self) -> None:
        assert self.pipeline is not None
        while True:
            try:
                now = datetime.now().astimezone()
                source_map = {
                    source.key: source
                    for source in self.sources
                    if source.enabled and source.schedules
                }
                targets = await self._scheduled_targets(source_map)
                for source in source_map.values():
                    due: list[tuple[dict[str, object], str]] = []
                    for target in targets:
                        if target["source_key"] != source.key:
                            continue
                        slot = schedule_slot(
                            now,
                            source.schedules,
                            source.schedule_time,
                            float(target["subscribed_at"]),
                        )
                        if slot and target["last_slot"] != slot:
                            due.append((target, slot))
                    if due:
                        await self._send_scheduled(source, due)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Smart Collector 定时任务循环异常")
            await asyncio.sleep(20)

    async def _send_scheduled(
        self,
        source: SourceConfig,
        due: list[tuple[dict[str, object], str]],
    ) -> None:
        assert self.pipeline is not None
        try:
            asset = await self.pipeline.collect(source, None, f"schedule:{source.key}:{due[0][1]}")
            await self._summarize(source, asset)
        except Exception:
            logger.exception("定时采集 %s 抓取失败", source.name)
            return
        sent = False
        for target, slot in due:
            try:
                chain = MessageChain(
                    chain=self._build_chain(
                        source,
                        asset,
                        str(target["sender_id"]),
                        str(target["sender_name"]),
                    )
                )
                delivered = await self.context.send_message(str(target["umo"]), chain)
                if delivered is False:
                    raise RuntimeError("AstrBot 未找到可发送的目标平台")
                sent = True
                if target.get("configured_group"):
                    await self.pipeline.cache.mark_schedule_slot(
                        source.key, str(target["umo"]), slot
                    )
                else:
                    await self.pipeline.cache.mark_slot(source.key, str(target["umo"]), slot)
            except Exception:
                logger.exception("定时采集 %s 向 %s 发送失败", source.name, target["umo"])
        if sent:
            await self.pipeline.mark_sent(source, asset, self.cache_days)

    async def _scheduled_targets(
        self, source_map: dict[str, SourceConfig]
    ) -> list[dict[str, object]]:
        assert self.pipeline is not None
        targets = {
            (item["source_key"], item["umo"]): item
            for item in await self.pipeline.cache.subscriptions()
            if item["source_key"] in source_map
        }
        platform_id = self._onebot_platform_id()
        if not platform_id:
            return list(targets.values())
        for source in source_map.values():
            for group_id in source.target_qq_groups:
                umo = f"{platform_id}:GroupMessage:{group_id}"
                key = (source.key, umo)
                if key in targets:
                    continue
                state = await self.pipeline.cache.schedule_state(source.key, umo)
                targets[key] = {
                    "source_key": source.key,
                    "umo": umo,
                    "sender_id": "",
                    "sender_name": "",
                    "subscribed_at": state["first_seen"],
                    "last_slot": state["last_slot"],
                    "configured_group": True,
                }
        return list(targets.values())

    def _onebot_platform_id(self) -> str:
        manager = getattr(self.context, "platform_manager", None)
        for platform in getattr(manager, "platform_insts", ()):
            try:
                metadata = platform.meta()
            except Exception:
                continue
            if getattr(metadata, "name", "") == "aiocqhttp":
                platform_id = str(getattr(metadata, "id", ""))
                if platform_id:
                    return platform_id
        return ""

    async def _cleanup_loop(self) -> None:
        assert self.pipeline is not None
        while True:
            try:
                await self.pipeline.cleanup(self.sources, self.cache_days)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Smart Collector 缓存清理失败")
            await asyncio.sleep(3600)

    def _match_sources(self, name: str) -> list[SourceConfig]:
        enabled = [source for source in self.sources if source.enabled]
        if not name.strip():
            return enabled
        needle = name.strip().casefold()
        exact = [source for source in enabled if source.name.casefold() == needle]
        return exact or [source for source in enabled if needle in source.name.casefold()]

    def _pixiv_sources(self) -> list[SourceConfig]:
        return [source for source in self.sources if source.enabled and source.template == "pixiv"]

    @staticmethod
    def _default_pixiv_source() -> SourceConfig:
        return SourceConfig(
            key="pixiv:__builtin__",
            template="pixiv",
            name="Pixiv 默认指令",
            enabled=True,
            url="https://app-api.pixiv.net/",
            content_types=(ContentType.IMAGE,),
            command="/pixiv",
            dedupe=0,
            image_to_pdf=False,
            compress=False,
            forward_mode="none",
            rate_limit=1.0,
            schedules=(),
            pixiv_age_mode="all",
            pixiv_quality="original",
            pixiv_r18_to_pdf=True,
        )

    @staticmethod
    def _temporary_source(url: str) -> SourceConfig:
        return SourceConfig.from_mapping(
            {
                "__template_key": "website",
                "name": "临时 URL",
                "enabled": True,
                "url": url,
                "content_types": [item.value for item in CONTENT_PRIORITY],
                "dedupe": -1,
                "forward_mode": "none",
                "rate_limit": -1,
            }
        )
