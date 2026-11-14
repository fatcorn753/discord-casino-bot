"""発掘品の効果（バフ）。持ち物に入っている間だけ、1個ごとに +1% の効果がつく（同じ種類を複数持てば、その分だけ重なる）。

出品中（預かり中）の品は、持ち物から出ているので、効かない。Discord と DB に依存しない純粋なロジック。
"""

import random

from infra import catalogs

_CFG = catalogs.relics()
BUFF_RATE = _CFG.buff_rate  # ★なしの発掘品1個ぶんの効果の大きさ
MERGE_COUNT = _CFG.merge_count  # 同じ発掘品をこの個数集めると、★が1つ増えた1個に合体できる
STAR_MAX = _CFG.star_max  # ★の最大。★が1つ増えるごとに、効果と目安の価値が MERGE_COUNT 倍（個数ぶんと同じ）
# 品のID → (効果の名前, 効果の説明)。効果の名前は、処理側が見分ける
BUFFS = {k: (v.effect, v.description) for k, v in _CFG.buffs.items()}


def split_id(item_id: str) -> tuple:
    """品のID → (★なしの品のID, ★の数)。ID は ★なしが `relic_fossil`、★n が `relic_fossil_sn`。"""
    head, sep, tail = item_id.rpartition("_s")
    if sep and tail.isdigit() and head in BUFFS:
        return head, int(tail)
    return item_id, 0


def star_id(base_id: str, star: int) -> str:
    """★なしの品のIDと★の数から、その品のID。"""
    return base_id if star <= 0 else f"{base_id}_s{star}"


def stars(star: int) -> str:
    return "★" * star


def buffs_of(held) -> dict:
    """持っている発掘品から、効果ごとの上がり幅 {効果の名前: 0.01 × 個数}。持っていない効果は入らない。

    held は {品のID: 個数}、または品のIDの並び（同じIDが複数あれば、その個数）。
    """
    counts = dict(held) if isinstance(held, dict) else {}
    if not isinstance(held, dict):
        for i in held:
            counts[i] = counts.get(i, 0) + 1
    out: dict = {}
    for item_id, qty in counts.items():
        base, star = split_id(item_id)
        if base in BUFFS and qty > 0:
            name = BUFFS[base][0]
            out[name] = round(out.get(name, 0.0) + BUFF_RATE * MERGE_COUNT**star * qty, 6)
    return out


def buff_text(item_id: str) -> str:
    """その発掘品の効果の説明（品の説明に付ける）。"""
    base, star = split_id(item_id)
    name, what = BUFFS[base]
    return f"持っている間、{what}が 1個ごとに +{BUFF_RATE * MERGE_COUNT**star:.0%}"


def chance_round(x: float, rng=None) -> int:
    """小数を、確率で切り上げ・切り捨てして整数にする（1% のような小さな上がり幅も、平均では、そのとおり効く）。"""
    rng = rng or random
    base = int(x)
    return base + (1 if rng.random() < x - base else 0)
