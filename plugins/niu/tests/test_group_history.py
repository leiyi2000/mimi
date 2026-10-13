import json

from providers.group_history import GroupHistoryTools
from core.tools import ToolContext


class FakeProvider:
    def __init__(self):
        self.searches = []

    async def resolve_member(self, group_id, query):
        return {
            "status": "unique",
            "matches": [{"user_id": 42, "display_name": query}],
        }

    async def search(self, group_id, arguments):
        self.searches.append((group_id, arguments))
        return {"data": []}


async def test_target_member_must_be_current_user_or_resolved():
    provider = FakeProvider()
    tools = GroupHistoryTools(provider)
    context = ToolContext(1, 10, 100, "caller")

    rejected = await tools.search(context, {"target_user_id": 42})
    assert rejected.is_error
    assert rejected.content == "GROUP_MEMBER_NOT_RESOLVED"

    resolved = await tools.resolve(context, {"query": "member"})
    assert json.loads(resolved.content)["status"] == "unique"
    accepted = await tools.search(context, {"target_user_id": 42})
    assert not accepted.is_error
    assert provider.searches[0][0] == 1
