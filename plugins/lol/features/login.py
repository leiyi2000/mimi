import logging

from napcat import Text, MessageEvent

from dispatcher import command, argument
from auth import (
    MlolAuth,
    OAuthError,
    QimeiClient,
    QimeiError,
    build_authorize_url,
    parse_callback,
)
from mlol import MlolClient, MlolError, RoleService


log = logging.getLogger(__name__)

# Wired by main.py at startup so command handlers share one client set.
_client: MlolClient | None = None
_auth: MlolAuth | None = None
_roles: RoleService | None = None
_qimei: QimeiClient | None = None
_admins: frozenset[str] = frozenset()


def setup(
    *,
    client: MlolClient,
    auth: MlolAuth,
    roles: RoleService,
    qimei: QimeiClient,
    admins: frozenset[str],
) -> None:
    global _client, _auth, _roles, _qimei, _admins
    _client = client
    _auth = auth
    _roles = roles
    _qimei = qimei
    _admins = admins


async def _require_admin(event: MessageEvent) -> bool:
    if str(event.user_id) in _admins:
        return True
    await event.send_msg(Text(text="该指令仅限管理员使用。"))
    return False


@command("LOL登录")
async def handle_login(event: MessageEvent) -> None:
    if not await _require_admin(event):
        return
    arg = argument(event)
    if arg:
        await _complete_login(event, arg)
    else:
        await _start_login(event)


@command("LOL登出")
async def handle_logout(event: MessageEvent) -> None:
    if not await _require_admin(event):
        return
    assert _auth
    await _auth.logout()
    await event.send_msg(Text(text="已清除共享掌盟登录态。"))


async def _start_login(event: MessageEvent) -> None:
    assert _qimei
    try:
        await _qimei.get()  # warm the device identity; surfaces service errors here
    except QimeiError as exc:
        await event.send_msg(Text(text=f"设备标识初始化失败：{exc}"))
        return
    url = build_authorize_url(_qimei.device())
    await event.send_msg(
        Text(
            text=(
                "请打开以下链接授权掌盟：\n"
                f"{url}\n"
                "授权后复制浏览器跳转的完整链接，私聊发送"
                "「LOL登录<回调URL>」完成登录。授权信息不会记录到日志。"
            )
        )
    )


async def _complete_login(event: MessageEvent, callback_url: str) -> None:
    assert _qimei and _auth and _roles
    try:
        access_token, openid = parse_callback(callback_url)
    except OAuthError as exc:
        await event.send_msg(Text(text=str(exc)))
        return

    try:
        mcode = await _qimei.get()
    except QimeiError as exc:
        await event.send_msg(Text(text=f"设备标识获取失败：{exc}"))
        return

    try:
        session, mlol_user_id = await _auth.login_by_qq(
            mcode=mcode,
            openid=openid,
            access_token=access_token,
        )
    except MlolError as exc:
        await event.send_msg(Text(text=f"掌盟登录失败：{exc}"))
        return

    cookies = _auth.cookies(session, mlol_user_id)
    try:
        roles = await _roles.lol_roles(cookies)
    except MlolError as exc:
        await event.send_msg(Text(text=f"角色查询失败：{exc}"))
        return
    if not roles:
        await event.send_msg(Text(text="登录成功，但未查询到 LOL 端游角色。"))
        return

    role = roles[0]
    await _auth.save_role(
        session,
        uuid=role.uuid,
        scene=role.scene,
        area_id=role.area_id,
        area_name=role.area_name,
    )
    area = role.area_name or (str(role.area_id) if role.area_id else "未知大区")
    await event.send_msg(
        Text(
            text=(
                f"共享账号登录成功：{role.role_name or ''}（{area}）。"
                "所有查询将复用此登录态。"
            )
        )
    )
