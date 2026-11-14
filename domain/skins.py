"""ゲーム演出の変更品（スキン）の見た目。装備すると、結果画面のタイトル・飾り・地の文が変わる。

勝率・配当・演出のコマの数は変えない。どのゲームに効くかは games で決める。商品そのものは core/shop.py。
"""

from dataclasses import dataclass
from typing import Optional

from domain import shop
from infra import catalogs

CHINCHIRO, PACHINKO, BLACKJACK = "チンチロ", "パチンコ", "ブラックジャック"


@dataclass(frozen=True)
class Skin:
    item_id: str
    games: tuple  # 効くゲーム
    title_prefix: str  # 見出しの先頭に付ける飾り
    start_line: str  # ゲームの始まりの地の文
    win_line: str  # 勝ったときの一言
    lose_line: str  # 負けたときの一言
    dice_mark: str = ""  # チンチロ: サイコロ1つごとに付ける飾り
    role_win: Optional[dict] = None  # チンチロ: 自分の役ごとの、勝ったときの地の文（なければ、いつもの文）
    role_lose: Optional[dict] = None  # チンチロ: 負けたときの地の文（親の役、または自分のヒフミで引く）
    roll_comment: str = ""  # チンチロ: 1回振るごとに添える一言（{hand} に役の名前が入る）
    banner: str = ""  # パチンコ: 大当たり以上のときの見出し（なければ、いつもの見出し）


_CFG = catalogs.skins()
SKINS = {
    s.item_id: Skin(
        s.item_id,
        s.games,
        s.title_prefix,
        s.start_line,
        s.win_line,
        s.lose_line,
        dice_mark=s.dice_mark,
        role_win=s.role_win,
        role_lose=s.role_lose,
        roll_comment=s.roll_comment,
        banner=s.banner,
    )
    for s in _CFG.skins
}
SLOT_OF_GAME = dict(_CFG.slot_of_game)  # ゲームごとに見る装備の枠


def active_skin(profile: dict, game: str) -> Optional[Skin]:
    """そのゲームで、いま装備している変更品（なければ None）。別のゲーム用のものを装備していても、効かない。"""
    slot = SLOT_OF_GAME.get(game)
    skin_id = profile.get(slot) if slot else None
    skin = SKINS.get(skin_id) if isinstance(skin_id, str) else None
    return skin if skin is not None and game in skin.games else None


def check_catalog() -> list:
    """カタログと見た目が食い違っていないか（商品があるのに見た目がない・その逆）の問題の一覧。"""
    problems = [
        f"見た目がありません: {i.id}"
        for i in shop.CATALOG
        if i.kind in (shop.SKIN_DICE, shop.SKIN_TABLE) and i.id not in SKINS
    ]
    problems += [f"商品がありません: {k}" for k in SKINS if k not in shop.BY_ID]
    return problems
