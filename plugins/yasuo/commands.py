import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Literal

from jinja2 import TemplateError
from napcat import GroupMessageEvent, Image, NapCatError, Text
from tortoise.exceptions import BaseORMException

from original import OriginalMessageBuilder
from rendering.poster import RecallPosterBuilder, RecallPosterRenderer
from services import RecallService


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecallCommand:
    action: Literal["poster", "original"]
    count: int | None

    @property
    def valid(self) -> bool:
        return self.count is not None


class RecallCommandParser:
    poster_keyword = "群撤回"
    original_keyword = "原始消息"
    default_poster_count = 5
    default_original_index = 1
    max_poster_count = 10

    def parse(self, event: GroupMessageEvent) -> RecallCommand | None:
        if len(event.message) != 1 or not isinstance(event.message[0], Text):
            return None
        text = event.message[0].text.strip()
        if text.startswith(self.original_keyword):
            action: Literal["poster", "original"] = "original"
            keyword = self.original_keyword
            default = self.default_original_index
        elif text.startswith(self.poster_keyword):
            action = "poster"
            keyword = self.poster_keyword
            default = self.default_poster_count
        else:
            return None
        argument = text[len(keyword) :].strip()
        if not argument:
            return RecallCommand(action=action, count=default)
        if len(argument.split()) != 1 or not argument.isdecimal():
            return RecallCommand(action=action, count=None)
        count = int(argument)
        if count < 1 or (action == "poster" and count > self.max_poster_count):
            return RecallCommand(action=action, count=None)
        return RecallCommand(action=action, count=count)


class RecallCommandHandler:
    def __init__(
        self,
        recall_service: RecallService,
        poster_builder: RecallPosterBuilder,
        poster_renderer: RecallPosterRenderer,
        original_builder: OriginalMessageBuilder | None = None,
        parser: RecallCommandParser | None = None,
    ):
        self.recall_service = recall_service
        self.poster_builder = poster_builder
        self.poster_renderer = poster_renderer
        self.original_builder = original_builder or OriginalMessageBuilder()
        self.parser = parser or RecallCommandParser()

    async def handle(self, event: GroupMessageEvent) -> bool:
        command = self.parser.parse(event)
        if command is None:
            return False
        if not command.valid:
            usage = (
                "用法：原始消息 [正整数序号]"
                if command.action == "original"
                else "用法：群撤回 [1-10]"
            )
            await event.send_msg(Text(text=usage))
            return True

        if command.action == "original":
            try:
                record = await self.recall_service.get_for_group_position(
                    group_id=event.group_id,
                    self_id=event.self_id,
                    position=command.count or 1,
                )
            except BaseORMException:
                log.exception("recall query failed: group_id=%s", event.group_id)
                await event.send_msg(Text(text="查询撤回消息失败，请稍后重试"))
                return True
            if record is None:
                await event.send_msg(
                    Text(text=f"本群没有第 {command.count} 条撤回消息")
                )
                return True
            segments = await self.original_builder.build(record)
            try:
                await event.send_msg(segments)
            except NapCatError:
                log.exception(
                    "original recall send failed: group_id=%s",
                    event.group_id,
                )
                await event.send_msg(Text(text="发送原消息失败，请稍后重试"))
            return True

        try:
            records = await self.recall_service.list_for_group(
                group_id=event.group_id,
                self_id=event.self_id,
                limit=command.count or self.parser.default_poster_count,
            )
        except BaseORMException:
            log.exception("recall query failed: group_id=%s", event.group_id)
            await event.send_msg(Text(text="查询撤回消息失败，请稍后重试"))
            return True

        if not records:
            await event.send_msg(Text(text="本群暂无撤回记录"))
            return True

        try:
            poster, assets = await self.poster_builder.build(records)
            png = await asyncio.to_thread(
                self.poster_renderer.render,
                poster,
                assets,
            )
        except (OSError, RuntimeError, TemplateError, ValueError):
            log.exception("recall poster failed: group_id=%s", event.group_id)
            await event.send_msg(Text(text="生成撤回消息图片失败，请稍后重试"))
            return True

        try:
            encoded = base64.b64encode(png).decode()
            await event.send_msg(Image(file=f"base64://{encoded}"))
        except NapCatError:
            log.exception("recall poster send failed: group_id=%s", event.group_id)
            try:
                await event.send_msg(Text(text="发送撤回消息图片失败，请稍后重试"))
            except NapCatError:
                log.exception(
                    "recall send error notification failed: group_id=%s",
                    event.group_id,
                )
        return True
