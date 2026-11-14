"""ゲーム演出の変更品（スキン）の見た目。装備すると、結果画面のタイトル・飾り・地の文が変わる。

勝率・配当・演出のコマの数は変えない。どのゲームに効くかは games で決める。商品そのものは core/shop.py。
"""

from dataclasses import dataclass
from typing import Optional

from core import shop

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


SKINS = {
    s.item_id: s
    for s in (
        Skin(
            "skin_kuro",
            (CHINCHIRO,),
            "🕴️ ",
            "黒服が、静かに立ち会っている…",
            "黒服「…お見事です」",
            "黒服「…残念でしたな」",
            dice_mark="⚫",
            role_win={
                "pinzoro": "黒服「ピンゾロ…。5倍、お支払いします」 場が凍りつく…",
                "arashi": "黒服「嵐…。3倍です」 ざわ…ざわ…",
                "shigoro": "黒服「シゴロ。2倍です」 流れが来ている…",
            },
            role_lose={
                "pinzoro": "黒服「親のピンゾロ。5倍のお支払いを」 血の気が引いていく…",
                "arashi": "黒服「親の嵐。3倍です」 地下に悲鳴が響く…",
                "hifumi": "黒服「ヒフミ。倍のお支払いを」 地獄だ…",
            },
            roll_comment="　黒服「…{hand}」",
        ),
        Skin(
            "skin_gold_dice",
            (CHINCHIRO,),
            "✨ ",
            "黄金の賽が、丼の中で鈍く光る…",
            "✨ 黄金の賽が、まばゆく輝いた！",
            "黄金の賽は、冷たく転がった…",
            dice_mark="🟡",
            role_win={
                "pinzoro": "✨ 黄金のピンゾロ…ッ！！ 5倍…！ 賽が燃えるように輝く…！",
                "arashi": "✨ 黄金の嵐…ッ！ 3倍…！ 光が丼からあふれる…",
                "shigoro": "✨ 黄金のシゴロ…！ 2倍…。賽が笑っている…",
            },
            role_lose={"pinzoro": "親の黄金の賽がピンゾロ…！ 5倍払い…。金色の絶望…"},
        ),
        Skin(
            "skin_glass_dice",
            (CHINCHIRO,),
            "🩸 ",
            "赤い爪が、ガラスの賽をそっとつまむ…",
            "🩸 ガラスの賽が、高く澄んだ音で鳴いた…",
            "ガラスの賽に、ひびが入った…",
            dice_mark="🔴",
            role_win={
                "pinzoro": "🩸 ガラスの賽が、ピンゾロで砕け散った…ッ！ 5倍…！",
                "arashi": "🩸 嵐の音が、ガラスを震わせる…！ 3倍…！",
                "shigoro": "🩸 シゴロ…。赤い爪が、静かに笑う… 2倍…",
            },
            role_lose={"pinzoro": "🩸 親のピンゾロ…。ガラスが血に染まる… 5倍払い…"},
            roll_comment="　🩸",
        ),
        Skin(
            "skin_vip",
            (PACHINKO, BLACKJACK),
            "🥀 ",
            "黒薔薇のシートに、深く腰を沈める…",
            "🥀 黒薔薇が、静かに祝福している…",
            "🥀 黒薔薇の花びらが、一枚散った…",
            banner="# 🥀 黒薔薇の祝福 🥀",
        ),
        Skin(
            "skin_numa",
            (PACHINKO,),
            "🕳️ ",
            "…第3クギを、玉が抜けていく…",
            "🕳️ 沼が、ついに口を開けた！！",
            "🕳️ 沼は、すべてを飲み込んだ…",
            banner="# 🕳️ 沼が開いた…！！ 🕳️",
        ),
    )
}
SLOT_OF_GAME = {CHINCHIRO: "skin_dice", PACHINKO: "skin_table", BLACKJACK: "skin_table"}  # ゲームごとに見る装備の枠


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
