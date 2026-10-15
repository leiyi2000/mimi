from PIL import Image

from api import (
    AgentMemberResolveRequest,
    AgentMessageSearchRequest,
    resolve_agent_member,
    search_agent_messages,
)
from models import GroupMessage, GroupRecall


async def create_message(
    *,
    message_id: int,
    group_id: int = 1,
    user_id: int = 10,
    nickname: str = "测试用户",
    raw_message: str = "普通文本",
    local_image_paths: list[str | None] | None = None,
    time: int = 100,
    self_id: int = 999,
):
    return await GroupMessage.create(
        message_id=message_id,
        group_id=group_id,
        user_id=user_id,
        nickname=nickname,
        raw_message=raw_message,
        local_image_paths=local_image_paths,
        time=time,
        self_id=self_id,
    )


async def test_agent_search_is_group_bounded():
    await create_message(message_id=1, raw_message="大乱斗真好玩")
    await create_message(message_id=2, group_id=2, raw_message="大乱斗别的群")
    await create_message(
        message_id=3,
        raw_message="[CQ:image,file=x]",
        time=101,
    )
    await create_message(
        message_id=4,
        raw_message="机器人消息",
        user_id=999,
        time=102,
    )

    result = await search_agent_messages(
        AgentMessageSearchRequest(group_id=1, keywords=["大乱斗"]),
    )

    assert [item.message_id for item in result.data] == [1]
    assert result.data[0].text == "大乱斗真好玩"


async def test_agent_search_returns_bounded_images_and_keeps_omissions(tmp_path):
    image_path = tmp_path / "archived.png"
    Image.new("RGB", (2, 2), "red").save(image_path)
    missing_path = tmp_path / "missing.png"
    await create_message(
        message_id=20,
        raw_message="[CQ:image,file=missing]",
        local_image_paths=[str(missing_path)],
        time=102,
    )
    await create_message(
        message_id=21,
        raw_message="[CQ:image,file=one][CQ:image,file=two]",
        local_image_paths=[str(image_path), str(image_path)],
        time=101,
    )

    result = await search_agent_messages(
        AgentMessageSearchRequest(group_id=1, max_images=1),
    )

    assert [item.message_id for item in result.data] == [20, 21]
    assert result.data[0].text == "[CQ:image,file=missing]"
    assert result.data[0].images == []
    assert result.data[0].omitted_image_count == 1
    assert result.data[1].text == (
        "[CQ:image,file=one][CQ:image,file=two]"
    )
    assert len(result.data[1].images) == 1
    assert result.data[1].images[0].mime_type == "image/png"
    assert result.data[1].images[0].byte_size > 0
    assert result.data[1].omitted_image_count == 1


async def test_agent_search_preserves_bot_triggers_and_links():
    await create_message(
        message_id=30,
        raw_message="[CQ:at,qq=999] 南瓜 帮我总结 https://example.com/page",
        time=101,
    )
    await create_message(
        message_id=31,
        raw_message="原始消息 这是群友实际发送的内容",
        time=102,
    )

    result = await search_agent_messages(
        AgentMessageSearchRequest(group_id=1),
    )

    assert [item.message_id for item in result.data] == [31, 30]
    assert result.data[0].text == "原始消息 这是群友实际发送的内容"
    assert result.data[1].text == (
        "[CQ:at,qq=999] 南瓜 帮我总结 https://example.com/page"
    )


async def test_agent_search_excludes_recalled_messages():
    await create_message(message_id=10, raw_message="已撤回")
    await GroupRecall.create(
        message_id=10,
        group_id=1,
        user_id=10,
        operator_id=10,
        recalled_at=101,
    )

    result = await search_agent_messages(
        AgentMessageSearchRequest(group_id=1),
    )

    assert result.data == []


async def test_member_resolution_reports_ambiguity():
    await create_message(message_id=1, user_id=10, nickname="小王")
    await create_message(message_id=2, user_id=11, nickname="小王同学")

    result = await resolve_agent_member(
        AgentMemberResolveRequest(group_id=1, query="小王"),
    )

    assert result.status == "ambiguous"
    assert {item.user_id for item in result.matches} == {10, 11}
