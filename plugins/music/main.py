import logging
import asyncio
import argparse

import httpx
from dotenv import load_dotenv
from napcat import Text, Json, Image, MessageEvent, NapCatClient

from settings import *
from config import Config, _normalize_cookie
from auth import NeteaseAuth
from service import MusicService, MusicError


log = logging.getLogger(__name__)

QR_POLL_INTERVAL = 3


class LoginSession:
    """Caches the current QR while a background poll is running."""

    def __init__(self) -> None:
        self.unikey = ""
        self.b64 = ""
        self.active = False

    def reset(self) -> None:
        self.unikey = ""
        self.b64 = ""
        self.active = False


def command_argument(text: str, command: str) -> str | None:
    text = text.strip()
    prefix = text[: len(command)]
    if prefix.casefold() != command.casefold():
        return None
    return text[len(command) :].strip()


def parse_query(query: str) -> tuple[str, str]:
    if "-" in query:
        song_name, artist_name = query.split("-", 1)
    else:
        song_name, artist_name = query, ""
    return song_name.strip(), artist_name.strip()


async def handle_request(event, service: MusicService, text: str) -> None:
    song_name, artist_name = parse_query(text)
    try:
        ark = await service.request_card(song_name, artist_name)
    except MusicError as exc:
        await event.send_msg(Text(text=str(exc)))
        return
    await event.send_msg(Json(data=ark))


async def handle_login(event, auth: NeteaseAuth, session: LoginSession) -> None:
    if session.active:
        if session.b64:
            await event.send_msg(
                [
                    Text(
                        text="二维码仍有效，请尽快用网易云音乐 App 扫码（VIP 账号）："
                    ),
                    Image(file=f"base64://{session.b64}"),
                ]
            )
        else:
            await event.send_msg(Text(text="正在生成二维码，请稍候……"))
        return

    session.active = True  # claim before awaiting to avoid concurrent polls
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                unikey, qrimg = await auth.create_qr(client)
            except Exception:  # noqa: BLE001
                await event.send_msg(Text(text="生成二维码失败，请稍后再试。"))
                return

            session.unikey = unikey
            session.b64 = qrimg.split(",", 1)[1] if "," in qrimg else qrimg
            await event.send_msg(
                [
                    Text(
                        text="请用网易云音乐 App 扫码登录（VIP 账号），过期前可重发「网易云登录」再取："
                    ),
                    Image(file=f"base64://{session.b64}"),
                ]
            )

            while True:
                await asyncio.sleep(QR_POLL_INTERVAL)
                try:
                    code, cookie = await auth.check_qr(client, unikey)
                except Exception:  # noqa: BLE001, S112
                    continue
                if code == 800:
                    await event.send_msg(
                        Text(text="二维码已过期，请重新发送「网易云登录」。")
                    )
                    return
                if code == 803:
                    auth.save(cookie)
                    await event.send_msg(Text(text="登录成功，VIP 歌曲已可用。"))
                    return
    finally:
        session.reset()


async def handle_set_cookie(event, auth: NeteaseAuth, raw: str) -> None:
    cookie = _normalize_cookie(raw)
    if not cookie:
        await event.send_msg(Text(text="cookie 为空，用法：网易云登录 <MUSIC_U 值>"))
        return
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            data = await auth.profile(client, cookie)
        except Exception:  # noqa: BLE001
            await event.send_msg(Text(text="校验失败，请稍后再试。"))
            return
    if not data:
        await event.send_msg(Text(text="cookie 无效或已过期，请重新获取。"))
        return
    auth.save(cookie)
    nickname = (data.get("profile") or {}).get("nickname", "未知")
    await event.send_msg(Text(text=f"已登录：{nickname}，VIP 歌曲已可用。"))


async def handle_logout(event, auth: NeteaseAuth) -> None:
    if not auth.cookie:
        await event.send_msg(Text(text="当前未登录。"))
        return
    async with httpx.AsyncClient(timeout=20) as client:
        await auth.logout(client)
    await event.send_msg(Text(text="已退出登录，点歌恢复游客权限。"))


async def handle_profile(event, auth: NeteaseAuth) -> None:
    if not auth.cookie:
        await event.send_msg(Text(text="当前未登录，发送「网易云登录」扫码登录。"))
        return
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            data = await auth.profile(client)
        except Exception:  # noqa: BLE001
            await event.send_msg(Text(text="查询失败，请稍后再试。"))
            return

    if not data:
        await event.send_msg(Text(text="登录已失效，请重新发送「网易云登录」。"))
        return

    profile = data.get("profile") or {}
    account = data.get("account") or {}
    vip_map = {0: "普通用户", 10: "黑胶VIP", 11: "黑胶SVIP"}
    vip = vip_map.get(account.get("vipType"), f"类型{account.get('vipType')}")
    lines = [
        f"昵称：{profile.get('nickname', '未知')}",
        f"UID：{profile.get('userId', '未知')}",
        f"会员：{vip}",
    ]
    if profile.get("signature"):
        lines.append(f"签名：{profile['signature']}")
    await event.send_msg(Text(text="\n".join(lines)))


async def main(config: Config) -> None:
    auth = NeteaseAuth(
        netease_api=config.netease_api,
        cookie_file=config.cookie_file,
        initial_cookie=config.cookie,
    )
    service = MusicService(
        netease_api=config.netease_api,
        sign_api=config.sign_api,
        sign_key=config.sign_key,
        auth=auth,
    )
    login_session = LoginSession()
    url = f"ws://{config.napcat_host}:{config.napcat_port}/"

    while True:
        try:
            client = NapCatClient(url, config.napcat_token)
            async for event in client:
                match event:
                    case MessageEvent(message=[Text(text=text)]) if (
                        arg := command_argument(text, "网易云登录")
                    ) is not None:
                        if str(event.user_id) not in config.admins:
                            continue
                        if arg:
                            asyncio.create_task(handle_set_cookie(event, auth, arg))
                        else:
                            asyncio.create_task(
                                handle_login(event, auth, login_session)
                            )
                    case MessageEvent(message=[Text(text=text)]) if (
                        command_argument(text, "网易云登出") == ""
                    ):
                        if str(event.user_id) not in config.admins:
                            continue
                        asyncio.create_task(handle_logout(event, auth))
                    case MessageEvent(message=[Text(text=text)]) if (
                        command_argument(text, "网易云信息") == ""
                    ):
                        asyncio.create_task(handle_profile(event, auth))
                    case MessageEvent(message=[Text(text=text)]) if (
                        arg := command_argument(text, "点歌")
                    ) is not None:
                        await handle_request(event, service, arg)
        except Exception:  # noqa: BLE001
            import traceback

            log.error(traceback.format_exc())
            await asyncio.sleep(5)  # reconnect delay


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", help="load environment variables from this file")
    args = parser.parse_args()

    if args.env_file:
        load_dotenv(args.env_file)

    log.info("music plugin running")
    asyncio.run(main(Config.from_env()))
