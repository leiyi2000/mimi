from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from mlol import (
    RECENT_BATTLE_LIMIT,
    BattleDetail,
    BattlePage,
    Player,
    PlayerOverview,
)
from mlol.game_data import GAME_DATA

from .render import FONT_NAME


TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
BATTLE_RENDER_WIDTH = 1400
BATTLE_PAGE_WIDTH = BATTLE_RENDER_WIDTH // 2
DETAIL_RENDER_WIDTH = 1400
DETAIL_PAGE_WIDTH = DETAIL_RENDER_WIDTH // 2
_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
)


def _champion_image(champion_id: int, response_url: str) -> str:
    clean = response_url.split("?", 1)[0].lower()
    if clean.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return response_url
    champion = GAME_DATA.champion(champion_id)
    if champion is not None:
        return champion.image_url
    if champion_id:
        return f"https://down.qq.com/lolapp/lol/hero/head/{champion_id}.png"
    return ""


def battle_poster(
    player: Player,
    page: BattlePage,
    overview: PlayerOverview,
    details: dict[str, BattleDetail],
) -> tuple[str, list[str]]:
    assets = [player.avatar_url] if player.avatar_url else []
    if overview.rank_url:
        assets.append(overview.rank_url)
    battles = page.battles[:RECENT_BATTLE_LIMIT]
    champion_images = {
        battle.game_id: _champion_image(battle.champion_id, battle.champion_url)
        for battle in battles
    }
    detail_champions = {
        member.champion_id: _champion_image(member.champion_id, "")
        for detail in details.values()
        for member in detail.my_team + detail.opponent_team
    }
    item_images = {
        item: f"https://game.gtimg.cn/images/lol/act/img/item/{item}.png"
        for detail in details.values()
        for member in detail.my_team + detail.opponent_team
        for item in member.items
    }
    for battle in battles:
        if champion_images[battle.game_id]:
            assets.append(champion_images[battle.game_id])
    assets.extend(detail_champions.values())
    assets.extend(item_images.values())
    return (
        _env.get_template("battle.html.j2").render(
            player=player,
            battles=battles,
            champion_images=champion_images,
            detail_champions=detail_champions,
            item_images=item_images,
            details=details,
            overview=overview,
            page_width=BATTLE_PAGE_WIDTH,
            font_name=FONT_NAME,
        ),
        assets,
    )


def detail_poster(player: Player, detail: BattleDetail) -> tuple[str, list[str]]:
    members = detail.my_team + detail.opponent_team
    assets = [player.avatar_url] if player.avatar_url else []
    champion_images = {
        member.uuid: _champion_image(member.champion_id, "")
        for member in members
    }
    item_images = {
        item: f"https://game.gtimg.cn/images/lol/act/img/item/{item}.png"
        for member in members
        for item in member.items
    }
    augment_images = {
        augment.augment_id: augment.image_url
        for member in members
        for augment in member.augments
    }
    spell_images = {
        spell.spell_id: spell.image_url
        for member in members
        for spell in member.summoner_spells
    }
    assets.extend(champion_images.values())
    assets.extend(item_images.values())
    assets.extend(augment_images.values())
    assets.extend(spell_images.values())
    return (
        _env.get_template("detail.html.j2").render(
            player=player,
            detail=detail,
            target=detail.target,
            champion_images=champion_images,
            item_images=item_images,
            augment_images=augment_images,
            spell_images=spell_images,
            max_damage=max((member.damage for member in members), default=1),
            page_width=DETAIL_PAGE_WIDTH,
            font_name=FONT_NAME,
        ),
        assets,
    )
