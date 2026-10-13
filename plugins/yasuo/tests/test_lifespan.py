import asyncio
import contextvars

from api import AgentMessageSearchRequest, search_agent_messages
from main import NapCatSync, create_app
from models import GroupMessage


async def test_database_context_is_available_to_request_tasks(
    monkeypatch,
    tmp_path,
):
    async def idle_sync(self):
        await asyncio.Event().wait()

    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite://{tmp_path / 'yasuo.db'}",
    )
    monkeypatch.setattr(NapCatSync, "run", idle_sync)
    app = create_app()

    async with app.router.lifespan_context(app):
        await GroupMessage.create(
            message_id=1,
            group_id=2,
            user_id=3,
            nickname="测试用户",
            raw_message="数据库上下文",
            time=4,
            self_id=5,
        )
        request_task = asyncio.create_task(
            search_agent_messages(
                AgentMessageSearchRequest(group_id=2),
            ),
            context=contextvars.Context(),
        )

        result = await request_task

    assert [message.message_id for message in result.data] == [1]
