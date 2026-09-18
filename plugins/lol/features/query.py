import asyncio
import base64
import logging

import httpx
from napcat import Image, MessageEvent, Text

from auth import MlolAuth
from dispatcher import argument, command
from features.bind import get_binding
from features.rendering import fetch_assets, render_image
from features.rendering.posters import (
    BATTLE_RENDER_WIDTH,
    DETAIL_RENDER_WIDTH,
    battle_poster,
    detail_poster,
)
from mlol import (
    RECENT_BATTLE_LIMIT,
    BattleDetail,
    BattlePage,
    BattleService,
    MlolError,
    Player,
    PlayerOverview,
    PlayerSearch,
)


log = logging.getLogger(__name__)

_auth: MlolAuth | None = None
_search: PlayerSearch | None = None
_battles: BattleService | None = None
_recent: dict[int | str, tuple[Player, BattlePage]] = {}


def setup(
    *,
    auth: MlolAuth,
    search: PlayerSearch,
    battles: BattleService,
) -> None:
    global _auth, _search, _battles
    _auth = auth
    _search = search
    _battles = battles


async def _nickname(event: MessageEvent) -> str:
    nickname = argument(event)
    if nickname:
        return nickname
    binding = await get_binding(event.user_id)
    return binding.nickname if binding else ""


async def _player(event: MessageEvent) -> Player | None:
    assert _auth and _search
    nickname = await _nickname(event)
    if not nickname:
        await event.send_msg(
            Text(
                text=(
                    "请发送「LOL战绩 <昵称#编号>」，"
                    "或先发送「LOL绑定 <昵称#编号>」。"
                )
            )
        )
        return None
    session = await _auth.session()
    if session is None:
        await event.send_msg(Text(text="共享掌盟账号未登录或登录态已失效。"))
        return None
    try:
        await _auth.ensure_fresh(session)
        player = await _search.find(nickname, _auth.cookies(session))
    except (MlolError, httpx.HTTPError) as exc:
        log.warning("player search failed: %s", exc)
        await event.send_msg(Text(text=f"玩家搜索失败：{exc}"))
        return None
    if player is None:
        await event.send_msg(Text(text=f"未找到唯一匹配的 LOL 玩家「{nickname}」。"))
    return player


async def _send_poster(
    event: MessageEvent,
    built: tuple[str, list[str]],
    *,
    width: int | None = None,
) -> None:
    try:
        html, urls = built
        images = await fetch_assets(list(dict.fromkeys(urls)))
        if width is None:
            png = await asyncio.to_thread(render_image, html, images)
        else:
            png = await asyncio.to_thread(render_image, html, images, width)
    except Exception:
        log.exception("poster rendering failed")
        await event.send_msg(Text(text="海报生成失败，请稍后再试。"))
        return
    await event.send_msg(Image(file=f"base64://{base64.b64encode(png).decode()}"))


@command("LOL战绩")
async def handle_battle(event: MessageEvent) -> None:
    assert _auth and _battles
    _recent.pop(event.user_id, None)
    player = await _player(event)
    if player is None:
        return
    session = await _auth.session()
    assert session
    cookies = _auth.cookies(session)
    try:
        page = await _battles.list(
            player,
            cookies,
            self_uuid=session.mlol_user_id or "",
            self_scene=session.scene or "",
        )
    except (MlolError, httpx.HTTPError) as exc:
        await event.send_msg(Text(text=f"战绩查询失败：{exc}"))
        return
    if page.hidden:
        await event.send_msg(Text(text=f"玩家「{player.nickname}」已隐藏战绩。"))
        return
    overview_result, *detail_results = await asyncio.gather(
        _battles.overview(player, page.battles, cookies),
        *(
            _battles.detail(player, battle, cookies)
            for battle in page.battles
        ),
        return_exceptions=True,
    )
    overview = (
        overview_result
        if isinstance(overview_result, PlayerOverview)
        else PlayerOverview.from_dict({}, page.battles)
    )
    details: dict[str, BattleDetail] = {}
    for battle, result in zip(page.battles, detail_results):
        if isinstance(result, BattleDetail):
            details[battle.game_id] = result
        elif isinstance(result, Exception):
            log.warning("battle detail %s failed: %s", battle.game_id, result)
    _recent[event.user_id] = (player, page)
    await _send_poster(
        event,
        battle_poster(player, page, overview, details),
        width=BATTLE_RENDER_WIDTH,
    )


@command("LOL对局")
async def handle_detail(event: MessageEvent) -> None:
    assert _auth and _battles
    value = argument(event)
    try:
        index = int(value)
    except ValueError:
        index = 0
    recent = _recent.get(event.user_id)
    if not 1 <= index <= RECENT_BATTLE_LIMIT:
        await event.send_msg(
            Text(text=f"请发送「LOL对局 <1-{RECENT_BATTLE_LIMIT}>」。")
        )
        return
    if recent is None:
        await event.send_msg(Text(text="请先查询一次「LOL战绩 <昵称#编号>」。"))
        return
    player, page = recent
    if index > len(page.battles):
        await event.send_msg(Text(text=f"最近战绩只有 {len(page.battles)} 局。"))
        return
    session = await _auth.session()
    if session is None:
        await event.send_msg(Text(text="共享掌盟账号未登录或登录态已失效。"))
        return
    try:
        await _auth.ensure_fresh(session)
        detail = await _battles.detail(
            player,
            page.battles[index - 1],
            _auth.cookies(session),
        )
    except (MlolError, httpx.HTTPError) as exc:
        await event.send_msg(Text(text=f"对局详情查询失败：{exc}"))
        return
    await _send_poster(
        event,
        detail_poster(player, detail),
        width=DETAIL_RENDER_WIDTH,
    )
