from napcat import GroupRecallNoticeEvent

from models import GroupMessage, GroupRecall
from services import RecallService


def recall_event(
    message_id: int,
    *,
    group_id: int = 200,
    user_id: int = 300,
    operator_id: int | None = None,
    time: int = 1_760_000_000,
) -> GroupRecallNoticeEvent:
    return GroupRecallNoticeEvent(
        time=time,
        self_id=999,
        group_id=group_id,
        user_id=user_id,
        operator_id=operator_id if operator_id is not None else user_id,
        message_id=message_id,
    )


async def create_message(
    message_id: int,
    *,
    group_id: int = 200,
    user_id: int = 300,
    raw_message: str | None = "测试消息",
    local_image_paths: list[str] | None = None,
    time: int = 1_759_999_900,
) -> GroupMessage:
    return await GroupMessage.create(
        message_id=message_id,
        group_id=group_id,
        user_id=user_id,
        nickname=f"用户{user_id}",
        card=None,
        role="member",
        raw_message=raw_message,
        local_image_paths=local_image_paths,
        time=time,
        self_id=999,
    )


async def test_record_is_idempotent_and_preserves_first_event():
    service = RecallService()
    first = await service.record(recall_event(1))
    duplicate = await service.record(
        recall_event(1, group_id=201, operator_id=301, time=1_760_000_100)
    )

    assert duplicate.id == first.id
    assert await GroupRecall.all().count() == 1
    persisted = await GroupRecall.get(message_id=1)
    assert persisted.group_id == 200
    assert persisted.operator_id == 300


async def test_list_keeps_missing_messages_and_excludes_bot_before_limit():
    service = RecallService()
    await create_message(10, raw_message="第一条")
    await service.record(recall_event(10, time=100))
    await service.record(recall_event(11, time=200))
    await service.record(recall_event(12, user_id=999, time=300))

    records = await service.list_for_group(group_id=200, self_id=999, limit=2)

    assert [record.message_id for record in records] == [11, 10]
    assert records[0].sent_at is None
    assert records[0].raw_message is None
    assert records[1].raw_message == "第一条"


async def test_list_is_scoped_to_current_group():
    service = RecallService()
    await create_message(20, group_id=200)
    await create_message(21, group_id=201)
    await service.record(recall_event(20, group_id=200, time=100))
    await service.record(recall_event(21, group_id=201, time=200))

    records = await service.list_for_group(group_id=200, self_id=999, limit=10)

    assert [record.message_id for record in records] == [20]


async def test_get_for_group_position_uses_offset_without_loading_prefix():
    service = RecallService()
    for message_id, recalled_at in ((30, 100), (31, 200), (32, 300)):
        await create_message(message_id, raw_message=f"消息{message_id}")
        await service.record(recall_event(message_id, time=recalled_at))
    await service.record(recall_event(33, user_id=999, time=400))

    record = await service.get_for_group_position(
        group_id=200,
        self_id=999,
        position=3,
    )
    missing = await service.get_for_group_position(
        group_id=200,
        self_id=999,
        position=999,
    )

    assert record is not None
    assert record.message_id == 30
    assert missing is None
