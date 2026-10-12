from dataclasses import dataclass
from typing import Literal


ContentState = Literal["available", "message_not_stored", "content_not_stored"]
ImageState = Literal["available", "not_stored", "unreadable"]


@dataclass(frozen=True)
class ArchivedRecall:
    message_id: int
    group_id: int
    user_id: int
    operator_id: int
    recalled_at: int
    nickname: str | None
    card: str | None
    raw_message: str | None
    local_image_paths: tuple[str | None, ...]
    sent_at: int | None


@dataclass(frozen=True)
class ParsedContent:
    text: str | None
    image_count: int


@dataclass(frozen=True)
class RecallPosterImage:
    asset_key: str | None
    state: ImageState


@dataclass(frozen=True)
class RecallPosterItem:
    user_id: int
    display_name: str
    avatar_asset_key: str | None
    avatar_fallback: str
    sent_at: str
    recalled_at: str
    recalled_by_admin: bool
    text: str | None
    content_state: ContentState
    images: tuple[RecallPosterImage, ...]
    hidden_image_count: int


@dataclass(frozen=True)
class RecallPoster:
    generated_at: str
    items: tuple[RecallPosterItem, ...]
