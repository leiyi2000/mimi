import base64
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


async def test_search_returns_images_as_multimodal_tool_content():
    class ImageProvider(FakeProvider):
        async def search(self, group_id, arguments):
            return {
                "data": [
                    {
                        "message_id": 7,
                        "user_id": 10,
                        "display_name": "caller",
                        "time": 100,
                        "text": "",
                        "images": [
                            {
                                "mime_type": "image/png",
                                "data_base64": base64.b64encode(b"png").decode(),
                                "byte_size": 3,
                            }
                        ],
                        "omitted_image_count": 1,
                    }
                ]
            }

    tools = GroupHistoryTools(ImageProvider())
    result = await tools.search(ToolContext(1, 10, 100, "caller"), {})

    assert not result.is_error
    assert "cG5n" not in result.content
    payload = json.loads(result.content)
    assert payload["data"][0]["images"][0]["image_index"] == 0
    assert payload["data"][0]["omitted_image_count"] == 1
    api_content = result.as_api_content()
    assert isinstance(api_content, list)
    assert api_content[-1]["type"] == "image"
    assert api_content[-1]["source"]["data"] == "cG5n"


async def test_search_rejects_invalid_image_payload():
    class InvalidImageProvider(FakeProvider):
        async def search(self, group_id, arguments):
            return {
                "data": [
                    {
                        "message_id": 7,
                        "images": [
                            {
                                "mime_type": "image/png",
                                "data_base64": "invalid",
                                "byte_size": 3,
                            }
                        ],
                    }
                ]
            }

    result = await GroupHistoryTools(InvalidImageProvider()).search(
        ToolContext(1, 10, 100, "caller"),
        {},
    )

    assert result.is_error
    assert result.error_code == "GROUP_HISTORY_INVALID_RESPONSE"
