"""ゲーム演出の変更品（スキン）の見た目。装備すると、結果画面のタイトル・飾り・地の文が変わる。

勝率・配当・演出のコマの数は変えない。どのゲームに効くかは games で決める。商品そのものは domain/shop.py。
季節演出（seasons）は、期間中だけ、変更品を装備していない人の見た目になる（装備している人は、装備が優先）。
"""

import datetime
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from domain import shop
from domain.ports import get_catalogs
from domain.ports.catalog_types.shop import SkinContract

CHINCHIRO, PACHINKO, BLACKJACK = "チンチロ", "パチンコ", "ブラックジャック"


@dataclass(frozen=True)
class Skin:
    item_id: str
    games: tuple[str, ...]  # 効くゲーム
    title_prefix: str  # 見出しの先頭に付ける飾り
    start_line: str  # ゲームの始まりの地の文
    win_line: str  # 勝ったときの一言
    lose_line: str  # 負けたときの一言
    dice_mark: str = ""  # チンチロ: サイコロ1つごとに付ける飾り
    role_win: dict | None = None  # チンチロ: 自分の役ごとの、勝ったときの地の文（なければ、いつもの文）
    role_lose: dict | None = None  # チンチロ: 負けたときの地の文（親の役、または自分のヒフミで引く）
    roll_speaker: str = ""  # チンチロ: 1回振るごとに役名を囁く話者名（例: 黒服）
    roll_mark: str = ""  # チンチロ: 1回振るごとに末尾へ添える飾り記号
    banner: str = ""  # パチンコ: 大当たり以上のときの見出し（なければ、いつもの見出し）


@dataclass(frozen=True)
class Season:
    id: str
    name: str
    start: datetime.datetime  # この時刻を含む
    end: datetime.datetime  # この時刻を含まない
    skins: tuple[Skin, ...]
    daily_title: str = ""  # デイリーボーナスの見出し
    daily_line: str = ""  # デイリーボーナスの一言
    house_horse_icon: str = ""  # 競馬: 帝愛の馬（NPC）の絵

    def active(self, now: datetime.datetime) -> bool:
        return self.start <= now < self.end


def _skin(s: SkinContract) -> Skin:
    return Skin(
        s.item_id,
        s.games,
        s.title_prefix,
        s.start_line,
        s.win_line,
        s.lose_line,
        dice_mark=s.dice_mark,
        role_win=dict(s.role_win) if s.role_win is not None else None,
        role_lose=dict(s.role_lose) if s.role_lose is not None else None,
        roll_speaker=s.roll_speaker,
        roll_mark=s.roll_mark,
        banner=s.banner,
    )


_CFG = get_catalogs().skins()
SKINS = {s.item_id: _skin(s) for s in _CFG.skins}
SLOT_OF_GAME = dict(_CFG.slot_of_game)  # ゲームごとに見る装備の枠
SEASONS = tuple(
    Season(
        c.id,
        c.name,
        datetime.datetime.fromisoformat(c.start),
        datetime.datetime.fromisoformat(c.end),
        tuple(_skin(s) for s in c.skins),
        c.daily_title,
        c.daily_line,
        c.house_horse_icon,
    )
    for c in _CFG.seasons
)


def active_skin(profile: Mapping[str, Any], game: str) -> Skin | None:
    """そのゲームで、いま装備している変更品（なければ None）。別のゲーム用のものを装備していても、効かない。"""
    slot = SLOT_OF_GAME.get(game)
    skin_id = profile.get(slot) if slot else None
    skin = SKINS.get(skin_id) if isinstance(skin_id, str) else None
    return skin if skin is not None and game in skin.games else None


def season_skin(game: str, now: datetime.datetime) -> Skin | None:
    """いま期間中の季節演出の、そのゲームの見た目（なければ None）。"""
    season = active_season(now)
    return next((s for s in season.skins if game in s.games), None) if season else None


def active_season(now: datetime.datetime) -> Season | None:
    """いま期間中の季節演出（なければ None。期間が重なれば、先に書いたほう）。"""
    return next((s for s in SEASONS if s.active(now)), None)


def house_horse_icon(now: datetime.datetime) -> str:
    """競馬の帝愛の馬（NPC）の、いまの絵（季節演出の期間中は、その季節の絵。見た目だけで、強さは変わらない）。"""
    season = active_season(now)
    return season.house_horse_icon if season and season.house_horse_icon else ""


def skin_for(profile: Mapping[str, Any], game: str, now: datetime.datetime) -> Skin | None:
    """そのゲームで使う見た目。装備している変更品が優先で、なければ期間中の季節演出。"""
    return active_skin(profile, game) or season_skin(game, now)


def check_catalog() -> list[str]:
    """カタログと見た目が食い違っていないか（商品があるのに見た目がない・その逆）の問題の一覧。"""
    problems = [
        f"見た目がありません: {i.id}"
        for i in shop.CATALOG
        if i.kind in (shop.SKIN_DICE, shop.SKIN_TABLE) and i.id not in SKINS
    ]
    problems += [f"商品がありません: {k}" for k in SKINS if k not in shop.BY_ID]
    return problems
