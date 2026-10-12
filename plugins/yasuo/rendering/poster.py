from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pytakumi import html_to_pic

from content import RecallContentParser
from domain import (
    ArchivedRecall,
    RecallPoster,
    RecallPosterItem,
)
from rendering.assets import PosterAssetLoader
from services import GroupMemberResolver


RENDER_WIDTH = 1120
DEVICE_PIXEL_RATIO = 2
PAGE_WIDTH = RENDER_WIDTH // DEVICE_PIXEL_RATIO
FONT_NAME = "YasuoCJK"

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PLUGIN_DIR / "templates"
FONT_DIR = Path(__file__).resolve().parent / "fonts"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(("html",)),
)


class RecallPosterBuilder:
    def __init__(
        self,
        member_resolver: GroupMemberResolver,
        asset_loader: PosterAssetLoader | None = None,
        content_parser: RecallContentParser | None = None,
        timezone_name: str = "Asia/Shanghai",
    ):
        self.member_resolver = member_resolver
        self.asset_loader = asset_loader or PosterAssetLoader()
        self.content_parser = content_parser or RecallContentParser()
        self.timezone = ZoneInfo(timezone_name)

    async def build(
        self,
        records: tuple[ArchivedRecall, ...],
    ) -> tuple[RecallPoster, dict[str, bytes]]:
        missing_names = {
            record.user_id
            for record in records
            if not self._stored_name(record)
        }
        resolved_names = await self.member_resolver.resolve(
            records[0].group_id,
            missing_names,
        )
        avatar_keys, assets = await self.asset_loader.load_avatars(
            {record.user_id for record in records}
        )

        items = []
        for record in records:
            parsed = self.content_parser.parse(record.raw_message)
            images, image_assets, hidden_count = (
                await self.asset_loader.load_message_images(
                    record.message_id,
                    parsed.image_count,
                    record.local_image_paths,
                )
            )
            assets.update(image_assets)

            message_stored = record.sent_at is not None
            has_available_content = bool(parsed.text) or any(
                image.state == "available" for image in images
            )
            if not message_stored:
                content_state = "message_not_stored"
            elif has_available_content:
                content_state = "available"
            else:
                content_state = "content_not_stored"

            display_name = (
                self._stored_name(record)
                or self._visible_name(resolved_names.get(record.user_id))
                or "群成员"
            )
            items.append(
                RecallPosterItem(
                    user_id=record.user_id,
                    display_name=display_name,
                    avatar_asset_key=avatar_keys.get(record.user_id),
                    avatar_fallback=display_name[:1] or "群",
                    sent_at=self._format_time(record.sent_at),
                    recalled_at=self._format_time(record.recalled_at),
                    recalled_by_admin=record.operator_id != record.user_id,
                    text=parsed.text if message_stored else None,
                    content_state=content_state,
                    images=images if message_stored else (),
                    hidden_image_count=hidden_count if message_stored else 0,
                )
            )

        return (
            RecallPoster(
                generated_at=self._format_time(int(datetime.now().timestamp())),
                items=tuple(items),
            ),
            assets,
        )

    def _stored_name(self, record: ArchivedRecall) -> str | None:
        return self._visible_name(record.card) or self._visible_name(record.nickname)

    def _visible_name(self, value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip()
        if not normalized or not any(character.isalnum() for character in normalized):
            return None
        return normalized

    def _format_time(self, timestamp: int | None) -> str:
        if timestamp is None:
            return "未知"
        value = datetime.fromtimestamp(timestamp, tz=self.timezone)
        return value.strftime("%m-%d %H:%M")


class RecallPosterRenderer:
    def build_html(self, poster: RecallPoster) -> str:
        template = _env.get_template("recall.html")
        return template.render(
            poster=poster,
            page_width=PAGE_WIDTH,
            font_name=FONT_NAME,
        )

    def render(
        self,
        poster: RecallPoster,
        assets: dict[str, bytes],
    ) -> bytes:
        return html_to_pic(
            self.build_html(poster),
            width=RENDER_WIDTH,
            device_pixel_ratio=DEVICE_PIXEL_RATIO,
            images=assets,
            fonts=self._load_fonts() or None,
        )

    def _load_fonts(self) -> list[dict]:
        path = FONT_DIR / "NotoSansSC.ttf"
        if not path.exists():
            return []
        return [{"data": path.read_bytes(), "name": FONT_NAME}]
