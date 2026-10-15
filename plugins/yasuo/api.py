import base64
import os
from datetime import datetime
from io import BytesIO

import aiofiles
from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, HTTPException, Query
from PIL import Image, UnidentifiedImageError
from tortoise.expressions import Q

from models import GroupMessage, GroupRecall


router = APIRouter()


async def _read_image_b64(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    try:
        async with aiofiles.open(path, "rb") as f:
            content = await f.read()
        return base64.b64encode(content).decode("utf-8")
    except OSError:
        return None


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: int
    group_id: int
    user_id: int
    nickname: str
    card: str | None = None
    role: str | None = None
    raw_message: str | None = None
    images_base64: list[str] | None = None
    time: int
    self_id: int
    created_at: datetime


class MessageListResponse(BaseModel):
    total: int
    data: list[MessageResponse]


class AgentMessageSearchRequest(BaseModel):
    group_id: int
    user_id: int | None = None
    keywords: list[str] = Field(default_factory=list, max_length=5)
    after: int | None = None
    before: int | None = None
    limit: int = Field(default=30, ge=1, le=50)
    cursor: str | None = None
    max_chars: int = Field(default=8000, ge=100, le=30000)
    max_images: int = Field(default=4, ge=0, le=10)
    max_image_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=1024,
        le=20 * 1024 * 1024,
    )
    max_total_image_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=20 * 1024 * 1024,
    )


class AgentImage(BaseModel):
    mime_type: str
    data_base64: str
    byte_size: int


class AgentMessage(BaseModel):
    message_id: int
    user_id: int
    display_name: str
    time: int
    text: str
    images: list[AgentImage] = Field(default_factory=list)
    omitted_image_count: int = 0


class AgentMessageSearchResponse(BaseModel):
    data: list[AgentMessage]
    next_cursor: str | None = None


class AgentMemberResolveRequest(BaseModel):
    group_id: int
    query: str = Field(min_length=1, max_length=100)


class AgentMember(BaseModel):
    user_id: int
    display_name: str


class AgentMemberResolveResponse(BaseModel):
    status: str
    matches: list[AgentMember]


def _message_text(raw_message: str | None) -> str:
    return raw_message or ""


async def _read_agent_image(
    path: str,
    *,
    max_bytes: int,
) -> AgentImage | None:
    try:
        size = os.path.getsize(path)
        if size <= 0 or size > max_bytes:
            return None
        async with aiofiles.open(path, "rb") as file:
            content = await file.read(max_bytes + 1)
        if len(content) != size or len(content) > max_bytes:
            return None
        with Image.open(BytesIO(content)) as image:
            image_format = str(image.format or "").upper()
            image.verify()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
    mime_type = {
        "GIF": "image/gif",
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "WEBP": "image/webp",
    }.get(image_format)
    if mime_type is None:
        return None
    return AgentImage(
        mime_type=mime_type,
        data_base64=base64.b64encode(content).decode("ascii"),
        byte_size=len(content),
    )


@router.post("/", response_model=MessageListResponse)
async def reads(
    query: dict,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1),
):
    queryset = GroupMessage.filter(**query)

    total = await queryset.count()
    offset = (page - 1) * page_size

    data = []
    async for row in queryset.order_by("-time").offset(offset).limit(page_size):
        message = MessageResponse.model_validate(row)
        if row.local_image_paths:
            images = []
            for path in row.local_image_paths:
                if not path:
                    continue
                image_b64 = await _read_image_b64(path)
                if image_b64 is not None:
                    images.append(image_b64)
            if images:
                message.images_base64 = images
        data.append(message)

    return MessageListResponse(total=total, data=data)


@router.post(
    "/v1/agent/messages/search",
    response_model=AgentMessageSearchResponse,
    dependencies=[],
)
async def search_agent_messages(
    request: AgentMessageSearchRequest,
):
    query = Q(group_id=request.group_id)
    if request.user_id is not None:
        query &= Q(user_id=request.user_id)
    if request.after is not None:
        query &= Q(time__gte=request.after)
    if request.before is not None:
        query &= Q(time__lte=request.before)
    if request.cursor:
        try:
            cursor_time, cursor_id = map(int, request.cursor.split(":", 1))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid cursor") from exc
        query &= Q(time__lt=cursor_time) | Q(time=cursor_time, id__lt=cursor_id)
    for keyword in request.keywords:
        keyword = keyword.strip()
        if keyword:
            query &= Q(raw_message__contains=keyword[:100])

    recalled_ids = await GroupRecall.filter(
        group_id=request.group_id
    ).values_list("message_id", flat=True)
    queryset = GroupMessage.filter(query)
    if recalled_ids:
        queryset = queryset.exclude(message_id__in=list(recalled_ids))

    data: list[AgentMessage] = []
    chars = 0
    image_count = 0
    image_bytes = 0
    last_row = None
    async for row in queryset.order_by("-time", "-id").limit(request.limit * 3):
        if row.user_id == row.self_id:
            continue
        text = _message_text(row.raw_message)
        image_paths = list(row.local_image_paths or [])
        if not text and not image_paths:
            continue
        if chars + len(text) > request.max_chars:
            break
        images: list[AgentImage] = []
        omitted_image_count = 0
        for path in image_paths:
            remaining_bytes = request.max_total_image_bytes - image_bytes
            if not path or image_count >= request.max_images or remaining_bytes < 1:
                omitted_image_count += 1
                continue
            image = await _read_agent_image(
                str(path),
                max_bytes=min(request.max_image_bytes, remaining_bytes),
            )
            if image is None:
                omitted_image_count += 1
                continue
            images.append(image)
            image_count += 1
            image_bytes += image.byte_size
        data.append(
            AgentMessage(
                message_id=row.message_id,
                user_id=row.user_id,
                display_name=row.card or row.nickname,
                time=row.time,
                text=text,
                images=images,
                omitted_image_count=omitted_image_count,
            )
        )
        chars += len(text)
        last_row = row
        if len(data) >= request.limit:
            break
    next_cursor = (
        f"{last_row.time}:{last_row.id}"
        if last_row is not None and len(data) >= request.limit
        else None
    )
    return AgentMessageSearchResponse(data=data, next_cursor=next_cursor)


@router.post(
    "/v1/agent/members/resolve",
    response_model=AgentMemberResolveResponse,
)
async def resolve_agent_member(
    request: AgentMemberResolveRequest,
):
    query = request.query.strip().lower()
    rows = await GroupMessage.filter(group_id=request.group_id).order_by("-time").limit(500)
    matches: dict[int, AgentMember] = {}
    for row in rows:
        names = tuple(name for name in (row.card, row.nickname) if name)
        if any(query in name.lower() for name in names):
            matches.setdefault(
                row.user_id,
                AgentMember(
                    user_id=row.user_id,
                    display_name=row.card or row.nickname,
                ),
            )
    values = list(matches.values())[:10]
    status = "unique" if len(values) == 1 else "not_found" if not values else "ambiguous"
    return AgentMemberResolveResponse(status=status, matches=values)
