import json
from dataclasses import dataclass
from pathlib import Path


DATA_PATH = Path(__file__).with_name("game_data.json")
CHAMPION_IMAGE_ROOT = "https://game.gtimg.cn/images/lol/act/img/champion"


def _key(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


@dataclass(frozen=True)
class ChampionData:
    champion_id: int
    name: str
    alias: str
    title: str

    @property
    def image_url(self) -> str:
        return f"{CHAMPION_IMAGE_ROOT}/{self.alias}.png"


@dataclass(frozen=True)
class AugmentData:
    augment_id: int
    name: str
    slug: str
    resource: str
    image_url: str


@dataclass(frozen=True)
class SummonerSpellData:
    spell_id: int
    name: str
    image_url: str


class GameData:
    def __init__(self, payload: dict) -> None:
        champions = [
            ChampionData(
                champion_id=int(item["id"]),
                name=str(item["name"]),
                alias=str(item["alias"]),
                title=str(item["title"]),
            )
            for item in payload["champions"]
        ]
        augments = [
            AugmentData(
                augment_id=int(item["id"]),
                name=str(item["name"]),
                slug=str(item["slug"]),
                resource=str(item["resource"]),
                image_url=str(item["image"]),
            )
            for item in payload["augments"]
        ]
        spells = [
            SummonerSpellData(
                spell_id=int(item["id"]),
                name=str(item["name"]),
                image_url=str(item["image"]),
            )
            for item in payload["summoner_spells"]
        ]
        self._champions = {item.champion_id: item for item in champions}
        self._spells = {item.spell_id: item for item in spells}
        self._augments: dict[str, AugmentData] = {}
        for item in augments:
            keys = {
                str(item.augment_id),
                item.slug,
                item.resource,
                f"{item.resource}_large",
                f"{item.resource}_large.png",
            }
            for key in keys:
                self._augments[_key(key)] = item

    def champion(self, champion_id: int) -> ChampionData | None:
        return self._champions.get(champion_id)

    def augment(self, value: object) -> AugmentData | None:
        if isinstance(value, dict):
            candidates = (
                value.get("id"),
                value.get("augment_id"),
                value.get("resource_key"),
                value.get("resource"),
                value.get("icon"),
                value.get("name"),
            )
        else:
            candidates = (value,)
        for candidate in candidates:
            key = _key(candidate)
            key = key.rsplit("/", 1)[-1]
            if item := self._augments.get(key):
                return item
        return None

    def summoner_spell(self, spell_id: int) -> SummonerSpellData | None:
        return self._spells.get(spell_id)


GAME_DATA = GameData(json.loads(DATA_PATH.read_text(encoding="utf-8")))
