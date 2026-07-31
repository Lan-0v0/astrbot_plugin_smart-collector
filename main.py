from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools, register

from .smart_collector.config import DEFAULT_SUMMARY_PROMPT, load_sources, requested_types
from .smart_collector.models import (
    CONTENT_PRIORITY,
    CollectedAsset,
    ContentType,
    SourceConfig,
    normalize_types,
)
from .smart_collector.pipeline import CollectionError, CollectorPipeline
from .smart_collector.schedule import schedule_slot


@register(
    "astrbot_plugin_smart_collector",
    "Lan-0v0",
    "支持视频、音频、图片和文字的并发自适应采集插件",
    "v0.0.1",
)
class SmartCollectorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        self.config = config
        self.sources = load_sources(config)
        self.pipeline: CollectorPipeline | None = None
        self._tasks: list[asyncio.Task] = []

    async def initialize(self) -> None:
        data_dir = StarTools.get_data_dir("astrbot_plugin_smart_collector")
        self.pipeline = CollectorPipeline(
            data_dir,
            concurrency=int(self.config.get("concurrency", 4)),
            timeout=float(self.config.get("request_timeout", 30)),
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
        logger.info("Smart Collector v0.0.1 已加载，共 %d 个自定义爬取项", len(self.sources))

    @filter.command("采集", alias={"爬取", "抓取"})
    async def collect_command(self, event: AstrMessageEvent) -> MessageEventResult:
        """从已启用的数据源采集内容；可在指令后指定视频、音频、图片或文字。"""
        query = self._after_command(event.message_str)
        async for result in self._collect_and_reply(event, self.sources, query):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def custom_source_commands(self, event: AstrMessageEvent) -> MessageEventResult:
        """识别配置中每个自定义爬取项的专属指令。"""
        message = event.message_str.strip()
        source = next(
            (
                item
                for item in self.sources
                if item.enabled
                and item.command
                and (message == item.command or message.startswith(item.command + " "))
            ),
            None,
        )
        if not source:
            return
        event.stop_event()
        query = message[len(source.command) :].strip()
        async for result in self._collect_and_reply(event, [source], query):
            yield result

    @filter.llm_tool(name="smart_collect")
    async def smart_collect(
        self,
        event: AstrMessageEvent,
        query: str,
        source_name: str = "",
        content_types: list[str] | None = None,
    ) -> MessageEventResult:
        """按用户的自然语言要求，从配置的数据源采集视频、音频、图片或文字。

        Args:
            query(string): 用户对采集内容的自然语言要求
            source_name(string): 可选的数据源名称；留空时并发尝试所有已启用数据源
            content_types(array[string]): 可选内容类型，只能使用 video、audio、image、text
        """
        if not bool(self.config.get("natural_language_enabled", True)):
            yield event.plain_result("自然语言爬取已在插件配置中关闭。")
            return
        sources = self._match_sources(source_name)
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
            await self.pipeline.mark_sent(source, asset)
        except CollectionError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.exception("Smart Collector 处理请求失败")
            yield event.plain_result(f"采集失败：{exc}")

    async def _summarize(self, source: SourceConfig, asset: CollectedAsset) -> None:
        if (
            asset.content_type is not ContentType.TEXT
            or not source.summary_provider
            or not asset.text
        ):
            return
        prompt = source.summary_prompt or DEFAULT_SUMMARY_PROMPT
        try:
            response = await self.context.llm_generate(
                chat_provider_id=source.summary_provider,
                prompt=f"{prompt}\n\n<原文>\n{asset.text[:100_000]}\n</原文>",
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
        cache_label = "缓存" if asset.cached else "新抓取"
        content: list = [Comp.Plain(f"[{source.name}] {cache_label}\n")]
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
        if asset.origin_url:
            content.append(Comp.Plain(f"\n来源：{asset.origin_url}"))

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
                for subscription in await self.pipeline.cache.subscriptions():
                    source = source_map.get(subscription["source_key"])
                    if not source:
                        continue
                    slot = schedule_slot(
                        now,
                        source.schedules,
                        source.schedule_time,
                        float(subscription["subscribed_at"]),
                    )
                    if not slot or subscription["last_slot"] == slot:
                        continue
                    try:
                        asset = await self.pipeline.collect(
                            source, None, f"schedule:{subscription['umo']}"
                        )
                        await self._summarize(source, asset)
                        chain = MessageChain(
                            chain=self._build_chain(
                                source,
                                asset,
                                subscription["sender_id"],
                                subscription["sender_name"],
                            )
                        )
                        await self.context.send_message(subscription["umo"], chain)
                        await self.pipeline.mark_sent(source, asset)
                        await self.pipeline.cache.mark_slot(source.key, subscription["umo"], slot)
                    except Exception:
                        logger.exception("定时采集 %s 发送失败", source.name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Smart Collector 定时任务循环异常")
            await asyncio.sleep(20)

    async def _cleanup_loop(self) -> None:
        assert self.pipeline is not None
        while True:
            try:
                await self.pipeline.cleanup(self.sources)
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
    def _after_command(message: str) -> str:
        parts = message.strip().split(maxsplit=1)
        return parts[1] if len(parts) == 2 else ""
