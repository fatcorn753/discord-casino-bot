"""発掘品の効果（バフ）。持ち物に入っている間だけ、種類ごとに +1% の効果がつく（同じ種類を何個持っても、1回だけ）。

出品中（預かり中）の品は、持ち物から出ているので、効かない。Discord と DB に依存しない純粋なロジック。
"""

import random

BUFF_RATE = 0.01  # 1種類ごとの効果の大きさ
# 品のID → (効果の名前, 効果の説明)。効果の名前は、処理側が見分ける
BUFFS = {
    "relic_fossil": ("click", "地下作業の「働く」1回の稼ぎ"),
    "relic_bone": ("wage", "作業員の稼ぎ"),
    "relic_meteor": ("luck", "作業員の鉱石・発掘品の出やすさ"),
    "relic_vase": ("recovery", "地下作業の疲労の回復"),
    "relic_crown": ("daily", "デイリーボーナスの額"),
    "relic_tear": ("prize", "競馬の賞金（馬主として受け取る額）"),
}


def buffs_of(held) -> dict:
    """持っている発掘品（品のIDの集まり）から、効果ごとの上がり幅 {効果の名前: 0.01}。持っていない効果は入らない。"""
    return {BUFFS[i][0]: BUFF_RATE for i in held if i in BUFFS}


def buff_text(item_id: str) -> str:
    """その発掘品の効果の説明（品の説明に付ける）。"""
    name, what = BUFFS[item_id]
    return f"持っている間、{what}が +{BUFF_RATE:.0%}"


def chance_round(x: float, rng=None) -> int:
    """小数を、確率で切り上げ・切り捨てして整数にする（1% のような小さな上がり幅も、平均では、そのとおり効く）。"""
    rng = rng or random
    base = int(x)
    return base + (1 if rng.random() < x - base else 0)
