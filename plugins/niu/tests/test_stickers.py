import json
from datetime import date, timedelta

import pytest

from core.tools import ToolContext, ToolImage, ToolRegistry
from domain.stickers import StickerStore, StickerStoreError, StickerTools


async def test_image_sticker_collection_organization_and_send(tmp_path):
    store = StickerStore(tmp_path, max_items=10, daily_update_limit=10)
    context = ToolContext(
        group_id=1,
        user_id=2,
        message_id=3,
        nickname="tester",
        images=(ToolImage(b"image-data", "image/png", "图片"),),
    )

    sticker = await store.collect_image(
        context=context,
        image_index=0,
        summary="一只猫震惊地看着镜头",
        emotion="震惊",
        tags=("猫", "震惊"),
    )
    duplicate = await store.collect_image(
        context=context,
        image_index=0,
        summary="重复描述",
        emotion="",
        tags=(),
    )

    assert duplicate.id == sticker.id
    assert len(await store.find(1, "", 20)) == 1
    updated = await store.update(
        group_id=1,
        sticker_id=sticker.id,
        summary="猫猫震惊",
        emotion="惊讶",
        tags=("猫", "惊讶"),
    )
    assert updated.summary == "猫猫震惊"

    artifact = await store.artifact(1, sticker.id)
    assert artifact.kind == "image"
    assert artifact.data == b"image-data"
    assert (await store.find(1, "惊讶", 1))[0].use_count == 1

    assert await store.delete(1, sticker.id)
    assert await store.find(1, "", 20) == []


async def test_passive_marketface_image_is_deduplicated(tmp_path):
    store = StickerStore(tmp_path, max_items=10, daily_update_limit=10)
    image = ToolImage(b"marketface", "image/gif", "猫猫点头")

    first = await store.collect_passive_image(
        group_id=1,
        user_id=2,
        image=image,
    )
    duplicate = await store.collect_passive_image(
        group_id=1,
        user_id=3,
        image=image,
    )

    assert duplicate.id == first.id
    assert duplicate.summary == "猫猫点头"
    assert len(await store.find(1, "", 10)) == 1


async def test_mface_is_passively_deduplicated_and_reusable(tmp_path):
    store = StickerStore(tmp_path, max_items=10, daily_update_limit=10)
    kwargs = {
        "group_id": 1,
        "user_id": 2,
        "emoji_package_id": 10,
        "emoji_id": "cat",
        "key": "secret-key",
        "summary": "猫猫点头",
    }

    assert await store.collect_mface(**kwargs)
    assert not await store.collect_mface(**kwargs)
    assert not await store.collect_mface(
        **{**kwargs, "key": "refreshed-key"}
    )

    sticker = (await store.find(1, "点头", 1))[0]
    artifact = await store.artifact(1, sticker.id)
    payload = json.loads(artifact.data)
    assert artifact.kind == "mface"
    assert payload == {
        "emoji_package_id": 10,
        "emoji_id": "cat",
        "key": "refreshed-key",
        "summary": "猫猫点头",
    }


async def test_sticker_tools_use_only_current_group_and_message_images(tmp_path):
    store = StickerStore(tmp_path, max_items=10, daily_update_limit=10)
    registry = ToolRegistry()
    StickerTools(store).register(registry)
    context = ToolContext(
        group_id=8,
        user_id=9,
        message_id=10,
        nickname="tester",
        images=(ToolImage(b"gif-data", "image/gif", "动图"),),
    )

    collected = await registry.execute(
        tool_use_id="collect",
        name="collect_current_image_as_sticker",
        arguments={
            "image_index": 0,
            "summary": "开心挥手",
            "emotion": "开心",
            "tags": ["挥手", "开心"],
        },
        context=context,
    )
    sticker_id = collected.content.removeprefix("STICKER_COLLECTED id=")
    found = await registry.execute(
        tool_use_id="find",
        name="find_group_stickers",
        arguments={"query": "开心"},
        context=context,
    )
    sent = await registry.execute(
        tool_use_id="send",
        name="send_group_sticker",
        arguments={"sticker_id": sticker_id},
        context=context,
    )

    assert json.loads(found.content)[0]["id"] == sticker_id
    assert sent.content == "STICKER_READY"
    assert sent.artifacts[0].data == b"gif-data"


async def test_capacity_and_daily_update_limits_are_independent(tmp_path):
    current_day = date(2026, 10, 13)
    store = StickerStore(
        tmp_path,
        max_items=2,
        daily_update_limit=1,
        today=lambda: current_day,
        choose=lambda records: records[0],
    )
    first = {
        "group_id": 1,
        "user_id": 2,
        "emoji_package_id": 10,
        "emoji_id": "first",
        "key": "first-key",
        "summary": "第一个",
    }

    assert await store.collect_mface(**first)
    assert not await store.collect_mface(**first)
    await store.artifact(1, (await store.find(1, "", 1))[0].id)

    with pytest.raises(StickerStoreError, match="STICKER_DAILY_UPDATE_LIMIT"):
        await store.collect_mface(
            **{
                **first,
                "emoji_id": "second",
                "key": "second-key",
                "summary": "第二个",
            }
        )

    current_day += timedelta(days=1)
    assert await store.collect_mface(
        **{
            **first,
            "emoji_id": "second",
            "key": "second-key",
            "summary": "第二个",
        }
    )
    current_day += timedelta(days=1)
    assert await store.collect_mface(
        **{
            **first,
            "emoji_id": "third",
            "key": "third-key",
            "summary": "第三个",
        }
    )
    assert {
        sticker.summary for sticker in await store.find(1, "", 10)
    } == {"第二个", "第三个"}


async def test_daily_classification_limit_persists_and_resets(tmp_path):
    current_day = date(2026, 10, 14)
    store = StickerStore(
        tmp_path,
        max_items=10,
        daily_update_limit=10,
        daily_classification_limit=2,
        today=lambda: current_day,
    )

    assert await store.reserve_classification(1)
    assert await store.reserve_classification(1)
    assert not await store.reserve_classification(1)

    reloaded = StickerStore(
        tmp_path,
        max_items=10,
        daily_update_limit=10,
        daily_classification_limit=2,
        today=lambda: current_day,
    )
    assert not await reloaded.reserve_classification(1)

    current_day += timedelta(days=1)
    assert await reloaded.reserve_classification(1)


async def test_classification_updates_metadata_without_update_quota(tmp_path):
    store = StickerStore(
        tmp_path,
        max_items=10,
        daily_update_limit=1,
        daily_classification_limit=1,
    )
    sticker = await store.collect_passive_image(
        group_id=1,
        user_id=2,
        image=ToolImage(b"gif", "image/gif", "[动画表情]"),
    )

    assert await store.reserve_classification(1)
    updated = await store.apply_classification(
        group_id=1,
        sticker_id=sticker.id,
        summary="小猫挥手",
        emotion="开心",
        tags=("猫", "挥手"),
    )

    assert updated.summary == "小猫挥手"
    assert updated.emotion == "开心"
    assert updated.tags == ("猫", "挥手")


async def test_capacity_replacement_deletes_evicted_image_file(tmp_path):
    store = StickerStore(
        tmp_path,
        max_items=1,
        daily_update_limit=10,
        choose=lambda records: records[0],
    )
    first = await store.collect_passive_image(
        group_id=1,
        user_id=2,
        image=ToolImage(b"first", "image/gif", "第一个"),
    )
    first_path = (
        tmp_path / "stickers" / "groups" / "1" / first.file_name
    )

    second = await store.collect_passive_image(
        group_id=1,
        user_id=2,
        image=ToolImage(b"second", "image/gif", "第二个"),
    )

    assert not first_path.exists()
    assert [item.id for item in await store.find(1, "", 10)] == [second.id]


async def test_noop_update_and_missing_delete_do_not_consume_quota(tmp_path):
    store = StickerStore(tmp_path, max_items=10, daily_update_limit=2)
    context = ToolContext(
        group_id=1,
        user_id=2,
        message_id=3,
        nickname="tester",
        images=(ToolImage(b"image-data", "image/png", "图片"),),
    )
    sticker = await store.collect_image(
        context=context,
        image_index=0,
        summary="原描述",
        emotion="",
        tags=(),
    )

    await store.update(
        group_id=1,
        sticker_id=sticker.id,
        summary="原描述",
        emotion="",
        tags=(),
    )
    assert not await store.delete(1, "missing")
    assert await store.delete(1, sticker.id)


async def test_legacy_index_is_loaded_and_migrated_on_write(tmp_path):
    group_dir = tmp_path / "stickers" / "groups" / "1"
    group_dir.mkdir(parents=True)
    (group_dir / "index.json").write_text(
        json.dumps(
            [
                {
                    "id": "mface-legacy",
                    "kind": "mface",
                    "summary": "旧表情",
                    "emotion": "",
                    "tags": [],
                    "created_by": 2,
                    "created_at": 1,
                    "emoji_package_id": 10,
                    "emoji_id": "legacy",
                    "key": "legacy-key",
                }
            ]
        ),
        encoding="utf-8",
    )
    store = StickerStore(tmp_path, max_items=10, daily_update_limit=10)

    assert (await store.find(1, "", 1))[0].id == "mface-legacy"
    await store.update(
        group_id=1,
        sticker_id="mface-legacy",
        summary="已迁移",
        emotion="",
        tags=(),
    )

    payload = json.loads((group_dir / "index.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["quota"]["updates"] == 1
