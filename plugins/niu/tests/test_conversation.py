import asyncio
import re

from core.agent import AgentResponse
from core.config import Config
from core.conversation import ConversationStore, TopicRoute
from core.topic import TopicGateDecision
from core.tools import Artifact
from domain.memory import MemoryStore
from domain.message_content import InputImage, QuotedMessage, UserMessage


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(0.01)
        current = str(kwargs["messages"][-1]["content"][0]["text"])
        match = re.search(r"<content>(.*?)</content>", current, re.DOTALL)
        return AgentResponse(f"reply:{match.group(1) if match else current}")


class FakeTopicGate:
    def __init__(self, *actions):
        self.actions = iter(actions)
        self.calls = []

    async def classify(self, candidate):
        self.calls.append(candidate)
        return TopicGateDecision(next(self.actions), 0.95, "test")


async def test_prefix_matching(configured_env):
    config = Config.from_env()
    store = ConversationStore(
        config=config,
        agent=FakeAgent(),
        memory=MemoryStore(config.data_dir, max_items=1, max_chars=10, notes_max_chars=10),
    )

    assert store.parse_trigger("  南瓜：你好") == "你好"
    assert store.parse_trigger("@机器人 你好") is None
    await store.close()


async def test_group_member_can_absorb_then_continue_active_topic(
    configured_env,
):
    config = Config.from_env()
    agent = FakeAgent()
    gate = FakeTopicGate("absorb", "respond")
    store = ConversationStore(
        config=config,
        agent=agent,
        memory=MemoryStore(config.data_dir, max_items=1, max_chars=10, notes_max_chars=10),
        topic_gate=gate,
    )
    sent = []

    async def send_text(text, mention_sender):
        sent.append(text)

    async def send_artifact(artifact):
        pass

    explicit = await store.route(
        group_id=1,
        user_id=10,
        nickname="甲",
        text="南瓜 帮我排查报错",
        explicit=True,
        has_reply=False,
        only_mentions_others=False,
        other_command=False,
        has_images=False,
        has_emojis=False,
    )
    assert await store.accept(
        message_id=1,
        group_id=1,
        user_id=10,
        nickname="甲",
        message=UserMessage("帮我排查报错"),
        send_text=send_text,
        send_artifact=send_artifact,
        route=explicit,
    )
    await store._groups[1].queue.join()
    topic_id = store._groups[1].active_topic.topic_id

    supplement = await store.route(
        group_id=1,
        user_id=11,
        nickname="乙",
        text="只在 Python 3.12 出现",
        explicit=False,
        has_reply=False,
        only_mentions_others=False,
        other_command=False,
        has_images=False,
        has_emojis=False,
    )
    assert supplement == TopicRoute("absorb", topic_id=topic_id)
    assert await store.accept(
        message_id=2,
        group_id=1,
        user_id=11,
        nickname="乙",
        message=UserMessage("只在 Python 3.12 出现"),
        send_text=send_text,
        send_artifact=send_artifact,
        route=supplement,
    )
    await store._groups[1].queue.join()
    assert len(agent.calls) == 1

    question = await store.route(
        group_id=1,
        user_id=11,
        nickname="乙",
        text="那应该先查哪里？",
        explicit=False,
        has_reply=False,
        only_mentions_others=False,
        other_command=False,
        has_images=False,
        has_emojis=False,
    )
    assert await store.accept(
        message_id=3,
        group_id=1,
        user_id=11,
        nickname="乙",
        message=UserMessage("那应该先查哪里？"),
        send_text=send_text,
        send_artifact=send_artifact,
        route=question,
    )
    await store._groups[1].queue.join()

    assert len(agent.calls) == 2
    assert "只在 Python 3.12 出现" in str(agent.calls[1]["messages"])
    assert [call.nickname for call in gate.calls] == ["乙", "乙"]
    await store.close()


async def test_new_explicit_topic_receives_previous_checkpoint(configured_env):
    config = Config.from_env()
    agent = FakeAgent()
    store = ConversationStore(
        config=config,
        agent=agent,
        memory=MemoryStore(config.data_dir, max_items=1, max_chars=10, notes_max_chars=10),
    )

    async def send_text(text, mention_sender):
        pass

    async def send_artifact(artifact):
        pass

    for message_id, user_id, nickname, text in (
        (1, 10, "甲", "排查 Python 报错"),
        (2, 11, "乙", "我补充一下，只在 3.12 出现"),
    ):
        route = await store.route(
            group_id=1,
            user_id=user_id,
            nickname=nickname,
            text=f"南瓜 {text}",
            explicit=True,
            has_reply=False,
            only_mentions_others=False,
            other_command=False,
            has_images=False,
            has_emojis=False,
        )
        assert await store.accept(
            message_id=message_id,
            group_id=1,
            user_id=user_id,
            nickname=nickname,
            message=UserMessage(text),
            send_text=send_text,
            send_artifact=send_artifact,
            route=route,
        )
        await store._groups[1].queue.join()

    assert len(agent.calls) == 2
    assert "目标：排查 Python 报错" in agent.calls[1]["conversation_context"]
    assert agent.calls[1]["messages"][-1]["content"][0]["text"].endswith(
        "<content>我补充一下，只在 3.12 出现</content>\n</group-message>"
    )
    assert "排查 Python 报错" not in str(agent.calls[1]["messages"])
    await store.close()


async def test_inactive_or_other_command_messages_are_ignored(
    configured_env,
):
    config = Config.from_env()
    gate = FakeTopicGate("respond")
    store = ConversationStore(
        config=config,
        agent=FakeAgent(),
        memory=MemoryStore(config.data_dir, max_items=1, max_chars=10, notes_max_chars=10),
        topic_gate=gate,
    )

    assert (
        await store.route(
            group_id=1,
            user_id=10,
            nickname="甲",
            text="普通消息",
            explicit=False,
            has_reply=False,
            only_mentions_others=False,
            other_command=False,
            has_images=False,
            has_emojis=False,
        )
    ).action == "ignore"

    async def send_text(text, mention_sender):
        pass

    async def send_artifact(artifact):
        pass

    explicit = TopicRoute("respond", explicit=True)
    assert await store.accept(
        message_id=1,
        group_id=1,
        user_id=10,
        nickname="甲",
        message=UserMessage("开始"),
        send_text=send_text,
        send_artifact=send_artifact,
        route=explicit,
    )
    await store._groups[1].queue.join()
    assert (
        await store.route(
            group_id=1,
            user_id=10,
            nickname="甲",
            text="点歌 测试",
            explicit=False,
            has_reply=False,
            only_mentions_others=False,
            other_command=True,
            has_images=False,
            has_emojis=False,
        )
    ).action == "ignore"
    assert not gate.calls

    store._groups[1].active_topic.idle_expires_at = 0
    assert (
        await store.route(
            group_id=1,
            user_id=11,
            nickname="乙",
            text="超时后的消息",
            explicit=False,
            has_reply=False,
            only_mentions_others=False,
            other_command=False,
            has_images=False,
            has_emojis=False,
        )
    ).action == "ignore"
    await store.close()


async def test_same_group_is_fifo_and_history_is_shared(configured_env):
    config = Config.from_env()
    agent = FakeAgent()
    store = ConversationStore(
        config=config,
        agent=agent,
        memory=MemoryStore(
            config.data_dir, max_items=10, max_chars=1000, notes_max_chars=100
        ),
    )
    sent = []

    async def send_text(text, mention_sender):
        sent.append(text)

    async def send_artifact(artifact):
        raise AssertionError("no artifact expected")

    for message_id, text in ((1, "first"), (2, "second")):
        assert await store.accept(
            message_id=message_id,
            group_id=1,
            user_id=message_id + 10,
            nickname=f"user-{message_id}",
            message=UserMessage(text),
            send_text=send_text,
            send_artifact=send_artifact,
        )
    await store._groups[1].queue.join()

    assert sent == ["reply:first", "reply:second"]
    assert agent.calls[1]["messages"][-3]["content"].endswith(
        "<text>first</text>\n</group-message>"
    )
    await store.close()


async def test_duplicate_message_is_ignored(configured_env):
    config = Config.from_env()
    store = ConversationStore(
        config=config,
        agent=FakeAgent(),
        memory=MemoryStore(config.data_dir, max_items=1, max_chars=10, notes_max_chars=10),
    )

    async def send_text(text, mention_sender):
        pass

    async def send_artifact(artifact):
        pass

    kwargs = {
        "message_id": 1,
        "group_id": 1,
        "user_id": 2,
        "nickname": "u",
        "message": UserMessage("hello"),
        "send_text": send_text,
        "send_artifact": send_artifact,
    }
    assert await store.accept(**kwargs)
    assert not await store.accept(**kwargs)
    await store._groups[1].queue.join()
    await store.close()


async def test_empty_input_has_usage_and_is_not_queued(configured_env):
    config = Config.from_env()
    store = ConversationStore(
        config=config,
        agent=FakeAgent(),
        memory=MemoryStore(config.data_dir, max_items=1, max_chars=10, notes_max_chars=10),
    )
    sent = []

    async def send_text(text, mention_sender):
        sent.append(text)

    async def send_artifact(artifact):
        pass

    accepted = await store.accept(
        message_id=1,
        group_id=1,
        user_id=2,
        nickname="u",
        message=UserMessage(""),
        send_text=send_text,
        send_artifact=send_artifact,
    )
    assert not accepted
    assert sent == ["用法：南瓜 <内容>"]
    await store.close()


async def test_current_images_are_multimodal_but_history_is_summary(
    configured_env,
):
    config = Config.from_env()
    agent = FakeAgent()
    store = ConversationStore(
        config=config,
        agent=agent,
        memory=MemoryStore(
            config.data_dir,
            max_items=1,
            max_chars=100,
            notes_max_chars=10,
        ),
    )

    async def send_text(text, mention_sender):
        pass

    async def send_artifact(artifact):
        pass

    message = UserMessage(
        "描述这张图",
        emojis=("微笑",),
        images=(InputImage(b"image-bytes", "image/png", "截图"),),
    )
    assert await store.accept(
        message_id=1,
        group_id=1,
        user_id=2,
        nickname='昵称"<',
        message=message,
        send_text=send_text,
        send_artifact=send_artifact,
    )
    await store._groups[1].queue.join()

    content = agent.calls[0]["messages"][-1]["content"]
    assert content[0]["type"] == "text"
    assert 'id="2"' in content[0]["text"]
    assert "昵称&quot;&lt;" in content[0]["text"]
    assert "[表情：微笑]" in content[0]["text"]
    assert content[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "aW1hZ2UtYnl0ZXM=",
    }
    assert store._groups[1].history[0].user_text == (
        "描述这张图\n[表情：微笑]\n[图片 0：截图]"
    )
    assert "aW1hZ2" not in store._groups[1].history[0].user_text
    await store.close()


async def test_quoted_images_are_available_to_tools(configured_env):
    config = Config.from_env()
    agent = FakeAgent()
    store = ConversationStore(
        config=config,
        agent=agent,
        memory=MemoryStore(
            config.data_dir,
            max_items=1,
            max_chars=100,
            notes_max_chars=10,
        ),
    )

    async def send_text(text, mention_sender):
        pass

    async def send_artifact(artifact):
        pass

    message = UserMessage(
        "参考这张图画一个新场景",
        quoted=QuotedMessage(
            message_id="quoted-1",
            sender_id=3,
            sender_nickname="原作者",
            text="人物参考",
            images=(
                InputImage(
                    b"quoted-image",
                    "image/jpeg",
                    "引用人物",
                    source="quoted",
                ),
            ),
        ),
    )
    assert await store.accept(
        message_id=1,
        group_id=1,
        user_id=2,
        nickname="用户",
        message=message,
        send_text=send_text,
        send_artifact=send_artifact,
    )
    await store._groups[1].queue.join()

    context = agent.calls[0]["context"]
    assert context.images == ()
    assert context.quoted_images[0].data == b"quoted-image"
    assert context.quoted_images[0].label == "引用人物"
    await store.close()


async def test_artifacts_are_sent_before_success_text(configured_env):
    class ArtifactAgent:
        async def run(self, **kwargs):
            return AgentResponse(
                "图片已生成",
                artifacts=(
                    Artifact("image", b"image", "image/png", "result"),
                ),
            )

    config = Config.from_env()
    store = ConversationStore(
        config=config,
        agent=ArtifactAgent(),
        memory=MemoryStore(
            config.data_dir,
            max_items=1,
            max_chars=10,
            notes_max_chars=10,
        ),
    )
    sent = []

    async def send_text(text, mention_sender):
        sent.append(("text", text, mention_sender))

    async def send_artifact(artifact):
        sent.append(("artifact", artifact.kind))

    await store.accept(
        message_id=1,
        group_id=1,
        user_id=2,
        nickname="u",
        message=UserMessage("生成图片"),
        send_text=send_text,
        send_artifact=send_artifact,
    )
    await store._groups[1].queue.join()

    assert sent == [
        ("artifact", "image"),
        ("text", "图片已生成", False),
    ]
    await store.close()


async def test_artifact_only_response_does_not_send_empty_text(configured_env):
    class ArtifactAgent:
        async def run(self, **kwargs):
            return AgentResponse(
                "",
                artifacts=(
                    Artifact("image", b"image", "image/png", "result"),
                ),
            )

    config = Config.from_env()
    store = ConversationStore(
        config=config,
        agent=ArtifactAgent(),
        memory=MemoryStore(
            config.data_dir,
            max_items=1,
            max_chars=10,
            notes_max_chars=10,
        ),
    )
    sent = []

    async def send_text(text, mention_sender):
        sent.append(("text", text, mention_sender))

    async def send_artifact(artifact):
        sent.append(("artifact", artifact.kind))

    await store.accept(
        message_id=1,
        group_id=1,
        user_id=2,
        nickname="u",
        message=UserMessage("生成图片"),
        send_text=send_text,
        send_artifact=send_artifact,
    )
    await store._groups[1].queue.join()

    assert sent == [("artifact", "image")]
    await store.close()


async def test_artifact_failure_does_not_send_success_text(configured_env):
    class ArtifactAgent:
        async def run(self, **kwargs):
            return AgentResponse(
                "文件已发送",
                artifacts=(
                    Artifact("file", b"data", "text/plain", "result.txt"),
                ),
            )

    config = Config.from_env()
    store = ConversationStore(
        config=config,
        agent=ArtifactAgent(),
        memory=MemoryStore(
            config.data_dir,
            max_items=1,
            max_chars=10,
            notes_max_chars=10,
        ),
    )
    sent = []

    async def send_text(text, mention_sender):
        sent.append(text)

    async def send_artifact(artifact):
        raise OSError("send failed")

    await store.accept(
        message_id=1,
        group_id=1,
        user_id=2,
        nickname="u",
        message=UserMessage("生成文件"),
        send_text=send_text,
        send_artifact=send_artifact,
    )
    await store._groups[1].queue.join()

    assert sent == ["部分内容已生成，但附件发送失败，请稍后重试。"]
    assert not store._groups[1].history
    await store.close()
