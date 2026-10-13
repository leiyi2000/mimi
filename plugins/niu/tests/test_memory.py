import pytest

from domain.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(
        tmp_path,
        max_items=10,
        max_chars=1000,
        notes_max_chars=100,
    )


async def test_group_and_user_memories_are_isolated(store):
    shared = await store.remember(
        group_id=1,
        user_id=10,
        scope="group",
        content="大家常玩大乱斗",
        message_id=1,
    )
    private = await store.remember(
        group_id=1,
        user_id=10,
        scope="group_user",
        content="我喜欢简短回答",
        message_id=2,
    )

    visible_owner = await store.visible(1, 10)
    visible_other = await store.visible(1, 11)
    assert {item.id for item in visible_owner} == {shared.id, private.id}
    assert {item.id for item in visible_other} == {shared.id}


async def test_forget_appends_tombstone(store):
    item = await store.remember(
        group_id=1,
        user_id=10,
        scope="group_user",
        content="偏好蓝色",
        message_id=1,
    )
    assert await store.forget(
        group_id=1,
        user_id=10,
        scope="group_user",
        memory_id=item.id,
        message_id=2,
    )
    assert await store.visible(1, 10) == []
    assert (store.root / "1" / "events.jsonl").exists()


async def test_store_does_not_classify_memory_content(store):
    item = await store.remember(
        group_id=1,
        user_id=10,
        scope="group_user",
        content="这是由模型判断后提交的密码提示",
        message_id=1,
    )

    assert item.content == "这是由模型判断后提交的密码提示"


async def test_personality_is_structured_and_persistent(store):
    profile = await store.update_personality(
        1, {"tone": "lively", "verbosity": "short"}
    )
    reloaded = await store.personality(1)

    assert profile.revision == 1
    assert reloaded.tone == "lively"
    assert reloaded.verbosity == "short"
    assert (await store.reset_personality(1)).tone == "neutral"


async def test_corrupted_file_is_quarantined(store):
    path = store.root / "1" / "personality.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="MEMORY_CORRUPTED"):
        await store.personality(1)
    assert list(path.parent.glob("personality.json.corrupt-*"))


async def test_memory_log_compacts_without_restoring_deleted_items(tmp_path):
    compacting = MemoryStore(
        tmp_path,
        max_items=10,
        max_chars=1000,
        notes_max_chars=100,
        compact_bytes=1,
    )
    item = await compacting.remember(
        group_id=1,
        user_id=10,
        scope="group",
        content="temporary",
        message_id=1,
    )
    await compacting.forget(
        group_id=1,
        user_id=10,
        scope="group",
        memory_id=item.id,
        message_id=2,
    )

    assert await compacting.visible(1, 10) == []
