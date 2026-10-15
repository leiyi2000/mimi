from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
from pathlib import Path

from napcat import (
    At,
    File,
    GroupMessageEvent,
    Image,
    MFace,
    NapCatClient,
    Record,
    Text,
)

from core.agent import AgentLoop
from core.config import Config, ConfigError
from core.conversation import ConversationStore, artifact_base64
from core.mimi import MimiCommandCatalog, MimiCommandTools
from core.prompts import PromptCatalog
from core.rendering import AutomaticReplyRenderer, RenderTools
from domain.memory import MemoryStore, MemoryTools
from providers.group_history import GroupHistoryProvider, GroupHistoryTools
from providers.image import ImageProvider
from providers.media import MediaDownloader, MediaError
from providers.music import MusicProvider
from providers.model import create_model_provider
from providers.sandbox import SandboxProvider, SandboxTools, WorkspaceTools
from providers.sticker_classifier import (
    StickerClassificationError,
    StickerClassifier,
)
from providers.web_search import AnySearchProvider, WebSearchTools
from adapters.napcat_context import NapCatContextAdapter
from adapters.qq_message import InputMessageError, QQMessageAdapter
import settings  # noqa: F401
from core.skills import MediaTools, ReplyTools, SkillRegistry
from domain.stickers import Sticker, StickerStore, StickerStoreError, StickerTools
from core.tools import AgentRuntimeTools, Artifact, ToolImage, ToolRegistry


log = logging.getLogger(__name__)


class NiuApplication:
    def __init__(self, config: Config) -> None:
        self.config = config
        skill_root = Path(__file__).resolve().parent / "skills"
        enabled = {
            "agent-runtime",
            "code",
            "memory",
            "render",
            "reply",
            "web-search",
            "workspace",
        }
        if config.group_history_enabled:
            enabled.add("group-history")
        if config.image_enabled:
            enabled.add("image")
        if config.music_enabled:
            enabled.add("music")
        if config.stickers_enabled:
            enabled.add("stickers")

        self.skills = SkillRegistry(skill_root)
        self.skills.load(enabled=enabled)
        mimi_commands = MimiCommandCatalog(skill_root.parent.parent).load()
        self.skills.register(MimiCommandCatalog.skill(mimi_commands))
        self.tools = ToolRegistry()
        AgentRuntimeTools(self.skills.activation_names()).register(self.tools)
        WebSearchTools(AnySearchProvider(config.anysearch_api_key)).register(self.tools)
        MimiCommandTools(mimi_commands).register(self.tools)
        RenderTools().register(self.tools)
        self.memory = MemoryStore(
            config.data_dir,
            max_items=config.memory_max_items,
            max_chars=config.memory_max_chars,
            notes_max_chars=config.personality_notes_max_chars,
            compact_bytes=config.memory_compact_bytes,
        )
        MemoryTools(self.memory).register(self.tools)

        downloader = MediaDownloader(
            timeout=config.request_timeout,
            max_bytes=config.media_max_bytes,
        )
        self.qq_messages = QQMessageAdapter(
            downloader=MediaDownloader(
                timeout=config.request_timeout,
                max_bytes=config.input_image_max_bytes,
            ),
            max_images=config.max_input_images,
            recent_image_ttl=config.message_cache_ttl,
        )
        image_provider = (
            ImageProvider(
                api_base_url=config.minimax_api_base_url,
                api_key=config.minimax_api_key,
                downloader=downloader,
                timeout=config.request_timeout,
            )
            if config.image_enabled
            else None
        )
        music_provider = (
            MusicProvider(
                api_base_url=config.minimax_api_base_url,
                api_key=config.minimax_api_key,
                downloader=downloader,
                timeout=max(config.request_timeout, 120),
            )
            if config.music_enabled
            else None
        )
        MediaTools(image_provider, music_provider).register(self.tools)
        ReplyTools().register(self.tools)
        self.stickers = StickerStore(
            config.data_dir,
            max_items=config.sticker_max_items,
            daily_update_limit=config.sticker_daily_updates,
            daily_classification_limit=config.sticker_daily_classifications,
        )
        if config.stickers_enabled:
            StickerTools(self.stickers).register(self.tools)
        if config.group_history_enabled:
            GroupHistoryTools(
                GroupHistoryProvider(
                    base_url=config.yasuo_api_url,
                    timeout=config.group_history_timeout,
                    max_items=config.group_history_max_items,
                    max_chars=config.group_history_max_chars,
                    max_images=config.max_input_images,
                    max_image_bytes=config.input_image_max_bytes,
                )
            ).register(self.tools)
        self.sandbox = SandboxProvider(
            base_url=config.sandbox_url,
            token=config.sandbox_token,
            request_timeout=config.sandbox_timeout,
        )
        SandboxTools(
            self.sandbox,
            max_timeout=config.sandbox_timeout,
        ).register(self.tools)
        WorkspaceTools(
            self.sandbox,
            max_timeout=config.sandbox_timeout,
        ).register(self.tools)
        self.skills.validate_tool_bindings(self.tools.tools_by_skill())

        self.model_provider = create_model_provider(
            provider=config.model_provider,
            api_key=config.model_api_key,
            base_url=config.model_base_url,
        )
        model_semaphore = asyncio.Semaphore(config.max_concurrency)
        self.sticker_classifier = (
            StickerClassifier(
                model_provider=self.model_provider,
                model=config.model,
                semaphore=model_semaphore,
                timeout=config.request_timeout,
            )
            if config.stickers_enabled
            else None
        )
        self._sticker_classification_tasks: dict[
            tuple[int, str], asyncio.Task[None]
        ] = {}
        self.agent = AgentLoop(
            model_provider=self.model_provider,
            config=config,
            prompts=PromptCatalog.load(Path(__file__).resolve().parent / "prompts"),
            skills=self.skills,
            tools=self.tools,
            model_semaphore=model_semaphore,
            automatic_renderer=AutomaticReplyRenderer(
                min_chars=config.auto_render_min_chars
            ),
        )
        self.conversations = ConversationStore(
            config=config,
            agent=self.agent,
            memory=self.memory,
        )

    async def start(self) -> None:
        if self.sticker_classifier is None:
            return
        for group_id, sticker, image in await self.stickers.pending_classifications():
            self._schedule_sticker_classification(
                group_id=group_id,
                sticker=sticker,
                image=image,
            )

    async def close(self) -> None:
        for task in self._sticker_classification_tasks.values():
            task.cancel()
        if self._sticker_classification_tasks:
            await asyncio.gather(
                *self._sticker_classification_tasks.values(),
                return_exceptions=True,
            )
        await self.conversations.close()
        await self.model_provider.close()
        await self.sandbox.close()

    async def handle(
        self,
        event: GroupMessageEvent,
        client: NapCatClient,
    ) -> None:
        if int(event.user_id) == int(event.self_id):
            return
        if self.config.stickers_enabled:
            await self._collect_stickers(event)
        text = self.qq_messages.text(event.message)
        prompt = self.conversations.parse_trigger(text)
        if prompt is None and self.qq_messages.mentions(event.message, event.self_id):
            prompt = text.strip()
        if prompt is None:
            self.qq_messages.remember_recent_images(
                event.message,
                group_id=int(event.group_id),
                user_id=int(event.user_id),
            )
            return

        async def send_text(
            content: str,
            mention_sender: bool = True,
        ) -> None:
            content = QQMessageAdapter.reply_text(content)
            message = [Text(text=content)]
            if mention_sender:
                message = [
                    At(qq=str(event.user_id)),
                    Text(text=f" {content}"),
                ]
            await event.send_msg(message)

        async def send_artifact(artifact: Artifact) -> None:
            encoded = artifact_base64(artifact)
            if artifact.kind == "image":
                await event.send_msg(Image(file=encoded))
            elif artifact.kind == "audio":
                await event.send_msg(Record(file=encoded))
            elif artifact.kind == "file":
                await event.send_msg(
                    File(
                        file=encoded,
                        name=self._artifact_filename(artifact),
                    )
                )
            elif artifact.kind == "mface":
                payload = json.loads(artifact.data)
                await event.send_msg(
                    MFace(
                        emoji_package_id=int(payload["emoji_package_id"]),
                        emoji_id=str(payload["emoji_id"]),
                        key=str(payload["key"]),
                        summary=str(payload["summary"]),
                    )
                )
            elif artifact.kind == "text":
                await event.send_msg(Text(text=artifact.data.decode("utf-8")))
            else:
                raise ValueError(f"unsupported artifact kind: {artifact.kind}")

        async def send_group_text(content: str) -> None:
            await event.send_msg(Text(text=content))

        try:
            message = await self.qq_messages.parse(
                event.message,
                trigger_prefix=self.config.trigger_prefix,
                group_id=int(event.group_id),
                bot_id=int(event.self_id),
                user_id=int(event.user_id),
                context=NapCatContextAdapter(client),
            )
        except InputMessageError as exc:
            await send_text(exc.user_message, True)
            return

        await self.conversations.accept(
            message_id=event.message_id,
            group_id=event.group_id,
            user_id=int(event.user_id),
            nickname=event.sender.card or event.sender.nickname,
            message=message,
            send_text=send_text,
            send_artifact=send_artifact,
            send_group_text=send_group_text,
            is_admin=event.sender.role in {"owner", "admin"},
        )

    async def _collect_stickers(self, event: GroupMessageEvent) -> None:
        for segment in event.message:
            try:
                if isinstance(segment, MFace):
                    await self.stickers.collect_mface(
                        group_id=event.group_id,
                        user_id=int(event.user_id),
                        emoji_package_id=segment.emoji_package_id,
                        emoji_id=segment.emoji_id,
                        key=segment.key,
                        summary=segment.summary,
                    )
                elif self.qq_messages.is_passive_sticker(segment):
                    image = await self.qq_messages.download_passive_sticker(segment)
                    tool_image = ToolImage(
                        image.data,
                        image.mime_type,
                        image.label,
                    )
                    sticker = await self.stickers.collect_passive_image(
                        group_id=event.group_id,
                        user_id=int(event.user_id),
                        image=tool_image,
                    )
                    self._schedule_sticker_classification(
                        group_id=event.group_id,
                        sticker=sticker,
                        image=tool_image,
                    )
            except (MediaError, StickerStoreError, ValueError):
                log.exception(
                    "sticker collection failed: group_id=%s message_id=%s",
                    event.group_id,
                    event.message_id,
                )

    def _schedule_sticker_classification(
        self,
        *,
        group_id: int,
        sticker: Sticker,
        image: ToolImage,
    ) -> None:
        classifier = getattr(self, "sticker_classifier", None)
        tasks = getattr(self, "_sticker_classification_tasks", None)
        if (
            classifier is None
            or tasks is None
            or not self.stickers.needs_classification(sticker)
        ):
            return
        key = (group_id, sticker.id)
        current = tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._classify_sticker(
                group_id=group_id,
                sticker_id=sticker.id,
                image=image,
            ),
            name=f"sticker-classifier-{group_id}-{sticker.id}",
        )
        tasks[key] = task

        def remove(completed: asyncio.Task[None]) -> None:
            if tasks.get(key) is completed:
                tasks.pop(key, None)

        task.add_done_callback(remove)

    async def _classify_sticker(
        self,
        *,
        group_id: int,
        sticker_id: str,
        image: ToolImage,
    ) -> None:
        classifier = self.sticker_classifier
        if classifier is None:
            return
        try:
            if not await self.stickers.reserve_classification(group_id):
                return
            metadata = await classifier.classify(image)
            await self.stickers.apply_classification(
                group_id=group_id,
                sticker_id=sticker_id,
                summary=metadata.summary,
                emotion=metadata.emotion,
                tags=metadata.tags,
            )
        except (
            StickerClassificationError,
            StickerStoreError,
            TimeoutError,
        ):
            log.exception(
                "sticker classification failed: group_id=%s sticker_id=%s",
                group_id,
                sticker_id,
            )
        except Exception:
            log.exception(
                "unexpected sticker classification failure: group_id=%s sticker_id=%s",
                group_id,
                sticker_id,
            )

    @staticmethod
    def _artifact_filename(artifact: Artifact) -> str:
        title = (artifact.title or "attachment").strip()
        safe_title = "".join(
            character
            for character in title
            if character.isalnum() or character in {"-", "_", "."}
        )[:80].lstrip(".")
        if not safe_title:
            safe_title = "attachment"
        if not Path(safe_title).suffix:
            safe_title += mimetypes.guess_extension(artifact.mime_type) or ".bin"
        return safe_title


async def run(config: Config) -> None:
    host = os.getenv("NAPCAT_HOST", "127.0.0.1")
    port = int(os.getenv("NAPCAT_PORT", "3001"))
    token = os.getenv("NAPCAT_TOKEN") or None
    application = NiuApplication(config)
    try:
        await application.start()
        while True:
            try:
                client = NapCatClient(f"ws://{host}:{port}/", token)
                async for event in client:
                    if not isinstance(event, GroupMessageEvent):
                        continue
                    try:
                        await application.handle(event, client)
                    except Exception:
                        log.exception(
                            "message handling failed: group_id=%s message_id=%s",
                            event.group_id,
                            event.message_id,
                        )
            except Exception:
                log.exception("NapCat connection ended")
                await asyncio.sleep(5)
    finally:
        await application.close()


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("invalid configuration: %s", exc)
        raise SystemExit(2) from exc
    log.info(
        "niu plugin running: provider=%s model=%s trigger_prefix=%s",
        config.model_provider,
        config.model,
        config.trigger_prefix,
    )
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
