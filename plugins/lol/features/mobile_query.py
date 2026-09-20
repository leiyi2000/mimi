import asyncio
import logging

import httpx
from napcat import MessageEvent, Text

from auth import MlolAuth
from dispatcher import argument, command
from features.bind import get_mobile_binding
from features.poster import send_poster
from features.rendering.posters import (
    MOBILE_BATTLE_RENDER_WIDTH,
    MOBILE_DETAIL_RENDER_WIDTH,
    mobile_battle_poster,
    mobile_detail_poster,
)
from mlol import (
    MOBILE_RECENT_BATTLE_LIMIT,
    MlolError,
    MobileBattleDetail,
    MobileBattlePage,
    MobileBattleService,
    MobilePlayer,
    MobilePlayerOverview,
    MobilePlayerSearch,
)


log = logging.getLogger(__name__)

_auth: MlolAuth | None = None
_search: MobilePlayerSearch | None = None
_battles: MobileBattleService | None = None
_recent: dict[int | str, tuple[MobilePlayer, MobileBattlePage]] = {}


def setup(
    *,
    auth: MlolAuth,
    search: MobilePlayerSearch,
    battles: MobileBattleService,
) -> None:
    global _auth, _search, _battles
    _auth = auth
    _search = search
    _battles = battles


async def _nickname(event: MessageEvent) -> str:
    nickname = argument(event)
    if nickname:
        return nickname
    binding = await get_mobile_binding(event.user_id)
    return binding.nickname if binding else ""


async def _player(event: MessageEvent) -> MobilePlayer | None:
    assert _auth and _search
    nickname = await _nickname(event)
    if not nickname:
        await event.send_msg(
            Text(
                text=(
                    "请发送「MLOL战绩 <手游昵称>」，"
                    "或先发送「MLOL绑定 <手游昵称>」。"
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
        log.warning("mobile player search failed: %s", exc)
        await event.send_msg(Text(text=f"手游玩家搜索失败：{exc}"))
        return None
    if player is None:
        await event.send_msg(Text(text=f"未找到唯一匹配的手游玩家「{nickname}」。"))
    return player


@command("MLOL战绩")
async def handle_mobile_battle(event: MessageEvent) -> None:
    assert _auth and _battles
    _recent.pop(event.user_id, None)
    player = await _player(event)
    if player is None:
        return
    session = await _auth.session()
    assert session
    cookies = _auth.cookies(session)
    try:
        page = await _battles.list(player, cookies)
    except (MlolError, httpx.HTTPError) as exc:
        await event.send_msg(Text(text=f"手游战绩查询失败：{exc}"))
        return
    if page.hidden:
        await event.send_msg(Text(text=f"玩家「{player.nickname}」已隐藏手游战绩。"))
        return
    overview_result, *detail_results = await asyncio.gather(
        _battles.overview(player, cookies),
        *(
            _battles.detail(player, battle, cookies)
            for battle in page.battles
            if battle.guid
        ),
        return_exceptions=True,
    )
    overview = (
        overview_result
        if isinstance(overview_result, MobilePlayerOverview)
        else MobilePlayerOverview.empty(player)
    )
    if isinstance(overview_result, Exception):
        log.warning("mobile overview failed: %s", overview_result)
    details: dict[str, MobileBattleDetail] = {}
    detail_battles = [battle for battle in page.battles if battle.guid]
    for battle, result in zip(detail_battles, detail_results):
        if isinstance(result, MobileBattleDetail):
            details[battle.guid] = result
        elif isinstance(result, Exception):
            log.warning("mobile battle detail failed: %s", result)
    _recent[event.user_id] = (player, page)
    await send_poster(
        event,
        mobile_battle_poster(player, page, overview, details),
        width=MOBILE_BATTLE_RENDER_WIDTH,
    )


@command("MLOL对局")
async def handle_mobile_detail(event: MessageEvent) -> None:
    assert _auth and _battles
    try:
        index = int(argument(event))
    except ValueError:
        index = 0
    recent = _recent.get(event.user_id)
    if not 1 <= index <= MOBILE_RECENT_BATTLE_LIMIT:
        await event.send_msg(
            Text(text=f"请发送「MLOL对局 <1-{MOBILE_RECENT_BATTLE_LIMIT}>」。")
        )
        return
    if recent is None:
        await event.send_msg(Text(text="请先查询一次「MLOL战绩 <手游昵称>」。"))
        return
    player, page = recent
    if index > len(page.battles):
        await event.send_msg(Text(text=f"最近手游战绩只有 {len(page.battles)} 局。"))
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
        await event.send_msg(Text(text=f"手游对局详情查询失败：{exc}"))
        return
    await send_poster(
        event,
        mobile_detail_poster(player, detail),
        width=MOBILE_DETAIL_RENDER_WIDTH,
    )
