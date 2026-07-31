from __future__ import annotations

import asyncio
import math
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
    normalize_types,
)
from .smart_collector.pipeline import CollectionError, CollectorPipeline
from .smart_collector.schedule import schedule_slot

COLLECT_COMMAND_USAGE = (
    "爬虫指令规范为：/爬取 [URL] [类型]\n其中URL为必须项，类型（视频/图片/音频/文字）为不必须项"
)
CUSTOM_SOURCE_COMMANDS: set[str] = set()
RESERVED_COMMANDS = {"/爬取", "爬取", "/抓取", "抓取"}


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


def _match_custom_source(
    message: str, sources: list[SourceConfig]
) -> tuple[SourceConfig | None, str]:
    text = message.strip()
    for source in sources:
        if not source.enabled:
            continue
        for command in _command_variants(source.command):
            if command in RESERVED_COMMANDS:
                continue
            if text == command:
                return source, ""
            if text.startswith(command + " "):
                return source, text[len(command) :].strip()
    return None, ""


class CustomSourceCommandFilter(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        message = event.get_message_str().strip()
        return any(
            message == command or message.startswith(command + " ")
            for command in CUSTOM_SOURCE_COMMANDS
        )


@register(
    "astrbot_plugin_smart_collector",
    "Lan-0v0",
    "支持视频、音频、图片和文字的并发自适应采集插件",
    "v0.1.3",
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
        self.pipeline: CollectorPipeline | None = None
        self._tasks: list[asyncio.Task] = []

    async def initialize(self) -> None:
        data_dir = StarTools.get_data_dir("astrbot_plugin_smart_collector")
        self.pipeline = CollectorPipeline(
            data_dir,
            concurrency=int(_config_number(self.config.get("concurrency", -1), -1, int)),
            timeout=float(_config_number(self.config.get("request_timeout", -1), -1, float)),
        )
        await self.pipeline.initialize()
        if not bool(self.config.get("natural_language_enabled", True)):
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
        logger.info("Smart Collector v0.1.3 已加载，共 %d 个自定义爬取项", len(self.sources))

    @filter.command("爬取", alias={"抓取"})
    async def collect_command(self, event: AstrMessageEvent) -> MessageEventResult:
        """爬取指定 URL；可指定视频、图片、音频或文字。"""
        query = self._after_command(event.message_str)
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
        if not bool(self.config.get("natural_language_enabled", True)):
            yield event.plain_result("自然语言爬取已在插件配置中关闭。")
            return
        query_url, query = split_url_request(query)
        target_url = url.strip() or query_url
        sources = (
            [self._temporary_source(target_url)] if target_url else self._match_sources(source_name)
        )
        explicit_types = normalize_types(content_types) if content_types else None
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
            yield result
            await self.pipeline.mark_sent(source, asset, self.cache_days)
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
        path = str(asset.local_path) if asset.local_path else ""
        if asset.mime_type in {"application/pdf", "application/zip"}:
            content.append(Comp.File(name=asset.title or Path(path).name, file=path))
        elif asset.content_type is ContentType.IMAGE:
            content.append(Comp.Image.fromFileSystem(path))
        elif asset.content_type is ContentType.VIDEO:
            content.append(Comp.Video.fromFileSystem(path))
        elif asset.content_type is ContentType.AUDIO:
            content.append(Comp.Record.fromFileSystem(path))
        else:
            text = asset.summary or asset.text
            content.append(Comp.Plain(text[:20_000]))
        if source.forward_mode == "none":
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

    @staticmethod
    def _after_command(message: str) -> str:
        parts = message.strip().split(maxsplit=1)
        return parts[1] if len(parts) == 2 else ""
