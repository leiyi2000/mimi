import logging

from napcat import Text, MessageEvent

from dispatcher import command, argument
from models import EABinding


log = logging.getLogger(__name__)


async def get_bound_ea_id(user_id: int | str) -> str | None:
    binding = await EABinding.get_or_none(user_id=int(user_id))
    return binding.ea_id if binding else None


@command("APEX绑定")
async def handle_bind(event: MessageEvent) -> None:
    ea_id = argument(event)
    if not ea_id:
        await event.send_msg(Text(text="用法：APEX绑定 <EA ID>"))
        return

    await EABinding.update_or_create(
        user_id=int(event.user_id),
        defaults={"ea_id": ea_id},
    )
    await event.send_msg(Text(text=f"已绑定 EA ID：{ea_id}"))
