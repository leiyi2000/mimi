import logging

from napcat import Text, MessageEvent

from dispatcher import command, argument
from models import LolBinding, MobileLolBinding


log = logging.getLogger(__name__)


async def get_binding(user_id: int | str) -> LolBinding | None:
    return await LolBinding.get_or_none(user_id=int(user_id))


async def get_mobile_binding(user_id: int | str) -> MobileLolBinding | None:
    return await MobileLolBinding.get_or_none(user_id=int(user_id))


@command("LOL绑定")
async def handle_bind(event: MessageEvent) -> None:
    nickname = argument(event)
    if not nickname:
        await event.send_msg(Text(text="用法：LOL绑定 <昵称#编号>"))
        return
    await LolBinding.update_or_create(
        user_id=int(event.user_id),
        defaults={"nickname": nickname},
    )
    await event.send_msg(Text(text=f"已保存查询昵称：{nickname}。发送「LOL战绩」即可查询。"))


@command("LOL解绑")
async def handle_unbind(event: MessageEvent) -> None:
    await LolBinding.filter(user_id=int(event.user_id)).delete()
    await event.send_msg(Text(text="已清除查询昵称。"))


@command("MLOL绑定")
async def handle_mobile_bind(event: MessageEvent) -> None:
    nickname = argument(event)
    if not nickname:
        await event.send_msg(Text(text="用法：MLOL绑定 <手游昵称>"))
        return
    await MobileLolBinding.update_or_create(
        user_id=int(event.user_id),
        defaults={"nickname": nickname},
    )
    await event.send_msg(
        Text(text=f"已保存手游查询昵称：{nickname}。发送「MLOL战绩」即可查询。")
    )


@command("MLOL解绑")
async def handle_mobile_unbind(event: MessageEvent) -> None:
    await MobileLolBinding.filter(user_id=int(event.user_id)).delete()
    await event.send_msg(Text(text="已清除手游查询昵称。"))
