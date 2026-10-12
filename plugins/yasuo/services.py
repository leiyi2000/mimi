import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from napcat import GroupRecallNoticeEvent, NapCatError

from domain import ArchivedRecall
from models import GroupMessage, GroupRecall


log = logging.getLogger(__name__)


class GroupMemberClient(Protocol):
    async def get_group_member_info(self, **kwargs: str) -> Mapping[str, Any] | None:
        ...


class RecallService:
    async def record(self, event: GroupRecallNoticeEvent) -> GroupRecall:
        existing = await GroupRecall.get_or_none(message_id=event.message_id)
        if existing is not None:
            incoming = (
                event.group_id,
                event.user_id,
                event.operator_id,
                event.time,
            )
            persisted = (
                existing.group_id,
                existing.user_id,
                existing.operator_id,
                existing.recalled_at,
            )
            if incoming != persisted:
                log.warning(
                    "duplicate recall differs: message_id=%s group_id=%s",
                    event.message_id,
                    event.group_id,
                )
            return existing

        return await GroupRecall.create(
            message_id=event.message_id,
            group_id=event.group_id,
            user_id=event.user_id,
            operator_id=event.operator_id,
            recalled_at=event.time,
        )

    async def list_for_group(
        self,
        group_id: int,
        self_id: int,
        limit: int,
    ) -> tuple[ArchivedRecall, ...]:
        recalls = await GroupRecall.filter(group_id=group_id).exclude(
            user_id=self_id
        ).order_by("-recalled_at").limit(limit)
        if not recalls:
            return ()

        message_ids = [recall.message_id for recall in recalls]
        messages = await GroupMessage.filter(
            group_id=group_id,
            message_id__in=message_ids,
        )
        by_id = {message.message_id: message for message in messages}

        records = []
        for recall in recalls:
            message = by_id.get(recall.message_id)
            records.append(self._archived_recall(recall, message))
        return tuple(records)

    async def get_for_group_position(
        self,
        group_id: int,
        self_id: int,
        position: int,
    ) -> ArchivedRecall | None:
        recall = await GroupRecall.filter(group_id=group_id).exclude(
            user_id=self_id
        ).order_by("-recalled_at").offset(position - 1).first()
        if recall is None:
            return None
        message = await GroupMessage.get_or_none(
            group_id=group_id,
            message_id=recall.message_id,
        )
        return self._archived_recall(recall, message)

    def _archived_recall(
        self,
        recall: GroupRecall,
        message: GroupMessage | None,
    ) -> ArchivedRecall:
        raw_paths = message.local_image_paths if message is not None else None
        paths = (
            tuple(
                path if isinstance(path, str) and path else None
                for path in raw_paths
            )
            if isinstance(raw_paths, list)
            else ()
        )
        return ArchivedRecall(
            message_id=recall.message_id,
            group_id=recall.group_id,
            user_id=recall.user_id,
            operator_id=recall.operator_id,
            recalled_at=recall.recalled_at,
            nickname=message.nickname if message is not None else None,
            card=message.card if message is not None else None,
            raw_message=message.raw_message if message is not None else None,
            local_image_paths=paths,
            sent_at=message.time if message is not None else None,
        )


class GroupMemberResolver:
    def __init__(self, client: GroupMemberClient):
        self.client = client

    async def resolve(
        self,
        group_id: int,
        user_ids: set[int],
    ) -> dict[int, str]:
        ordered_ids = sorted(user_ids)
        results = await asyncio.gather(
            *(self._resolve_one(group_id, user_id) for user_id in ordered_ids),
        )
        return {
            user_id: name
            for user_id, name in zip(ordered_ids, results)
            if name is not None
        }

    async def _resolve_one(self, group_id: int, user_id: int) -> str | None:
        try:
            member = await self.client.get_group_member_info(
                group_id=str(group_id),
                user_id=str(user_id),
            )
        except NapCatError:
            log.warning(
                "group member lookup failed: group_id=%s user_id=%s",
                group_id,
                user_id,
            )
            return None
        if not member:
            return None
        card = member.get("card")
        nickname = member.get("nickname")
        if isinstance(card, str) and card.strip():
            return card.strip()
        if isinstance(nickname, str) and nickname.strip():
            return nickname.strip()
        return None
