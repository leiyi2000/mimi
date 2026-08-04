import os
import base64
from datetime import datetime

import aiofiles
from pydantic import BaseModel
from fastapi import APIRouter, Query

from models import GroupMessage


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

    class Config:
        from_attributes = True


class MessageListResponse(BaseModel):
    total: int
    data: list[MessageResponse]


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
