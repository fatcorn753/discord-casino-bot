"""競馬の画面部品の集約ファサード（cards / betting / stable の再エクスポート）。"""

import discord as discord

from cogs.horse.betting import BetPanel, StakeModal
from cogs.horse.cards import (
    MEDAL,
    RaceButton,
    card_embed,
    card_view,
    race_title,
    record_text,
    refresh_entry_card,
    stat_short,
    when,
)
from cogs.horse.stable import AppearanceView, GoldCarrotView, NameModal, RetireView, Stable, StyleView
from cogs.shared.horse_display import bar as bar
from cogs.shared.horse_display import stat_lines as stat_lines

__all__ = [
    "MEDAL",
    "AppearanceView",
    "BetPanel",
    "GoldCarrotView",
    "NameModal",
    "RaceButton",
    "RetireView",
    "Stable",
    "StakeModal",
    "StyleView",
    "bar",
    "card_embed",
    "card_view",
    "discord",
    "race_title",
    "record_text",
    "refresh_entry_card",
    "stat_lines",
    "stat_short",
    "when",
]
