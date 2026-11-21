"""注文フロー共通の部品: 確認ダイアログ・セッション・口数/金額の計算と定数。"""

import math
import random
from typing import Optional

import discord

from cogs.shared.confirm import confirm_action, confirmed_reply, user_session
from domain import market
from domain.ports import get_catalogs
from domain.ports.session import ISessionGuard

RNG = random.SystemRandom()
CONFIRM_FROM = get_catalogs().market().market.confirm_from  # この額以上の売買は確認ボタンを挟む
MAX_MARGIN_NOTIONAL = 10_000_000  # 信用取引の、1回に建てられる総額の上限(#78)


def margin_quote(collateral: int, leverage: int, price: float) -> tuple[int, float, int]:
    """証拠金 × レバレッジ の総額で建てられる (口数, 実際の総額, 担保)。担保の式は repo（infra/db/margin.py）と同じ。"""
    units = int(collateral * leverage * market.UNIT // price)
    notional = units * price / market.UNIT
    return units, notional, math.ceil(round(notional / leverage, 6))


async def default_confirmed_reply(interaction: discord.Interaction, needed: bool, title: str, text: str, label: str):
    """大きな取引は、確認ボタンを出す。進んでよければ「返信に使う関数」を返し、やめたなら None を返す。"""
    return await confirmed_reply(interaction, needed, title, text, label, confirm_fn=confirm_action)


def units_from(have: int, percent: Optional[int]) -> Optional[int]:
    """売る・買い戻す口数。percent（1〜100）ならその割合（端数でも最低1口）、省略か100以上なら None（全部）。"""
    if percent is not None and 0 < percent < 100:
        return max(1, have * percent // 100)
    return None


def worth(have: int, units: Optional[int], price: float) -> float:
    """売買する分（units が None なら全部）の、いまの価格での金額。確認を挟むかの判定に使う。"""
    return (have if units is None else min(units, have)) * price // market.UNIT


async def run_trade(sessions: ISessionGuard, interaction: discord.Interaction, action) -> None:
    """売買（action(uid)）の間は「プレイ中」にして、ゲームの最中に残高を動かせないようにする。"""
    async with user_session(sessions, interaction, "いまは取引できません") as ok:
        if ok:
            await action(interaction.user.id)
