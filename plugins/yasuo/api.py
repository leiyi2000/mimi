import os
import base64
from typing import Optional
from datetime import datetime

from pydantic import BaseModel
from fastapi import APIRouter, Query

from models import GroupMessage


router = APIRouter()


class MessageResponse(BaseModel):
    id: int
    message_id: int
    group_id: int
    user_id: int
    nickname: str
    card: Optional[str] = None
    role: Optional[str] = None
    raw_message: Optional[str] = None
    images_base64: Optional[list[str]] = None
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
                if path and os.path.exists(path):
                    try:
                        with open(path, "rb") as f:
                            images.append(base64.b64encode(f.read()).decode("utf-8"))
                    except Exception:
                        pass
            if images:
                message.images_base64 = images
        data.append(message)

    return MessageListResponse(total=total, data=data)
