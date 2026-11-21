"""季節演出（catalogs/skins.yaml の seasons）: 期間中だけ、装備のない人の見た目とデイリーの文言が変わる。"""

import datetime

import pytest

from cogs.economy import embeds
from domain import skins
from domain.types import DailyResult
from infra.catalogs.shop import SeasonConfig, SkinConfig
from tests.fakes import must

pytestmark = pytest.mark.real_seasons

JST = datetime.timezone(datetime.timedelta(hours=9))
XMAS_EVE = datetime.datetime(2026, 12, 24, 21, 0, tzinfo=JST)
GAMES = ("チンチロ", "パチンコ", "ブラックジャック", "スロット", "ルーレット")


def at(*args) -> datetime.datetime:
    return datetime.datetime(*args, tzinfo=JST)


def test_christmas_covers_every_casino_game_until_the_end_of_the_25th():
    for game in GAMES:
        assert must(skins.season_skin(game, XMAS_EVE)).title_prefix == "🎄 "
    assert skins.season_skin("チンチロ", at(2026, 12, 25, 23, 59, 59)) is not None
    assert skins.season_skin("チンチロ", at(2026, 12, 26, 0, 0)) is None  # 26日の0時で終わる
    assert skins.season_skin("チンチロ", at(2026, 11, 20, 23, 59)) is None  # 始まる前
    assert skins.season_skin("チンチロ", at(2026, 11, 21, 0, 0)) is not None
    assert skins.season_skin("競馬", XMAS_EVE) is None  # カジノのゲームだけ


def test_an_equipped_skin_wins_over_the_season():
    assert must(skins.skin_for({"skin_dice": "skin_gold_dice"}, "チンチロ", XMAS_EVE)).item_id == "skin_gold_dice"
    assert must(skins.skin_for({}, "チンチロ", XMAS_EVE)).item_id == "season_xmas_dice"
    assert skins.skin_for({}, "チンチロ", at(2027, 1, 1)) is None
    # 別のゲーム用の変更品を装備していても、そのゲームでは季節演出になる
    assert must(skins.skin_for({"skin_table": "skin_numa"}, "ブラックジャック", XMAS_EVE)).item_id == (
        "season_xmas_blackjack"
    )


def test_daily_bonus_reads_as_a_present_from_santa_only_during_the_season():
    result = DailyResult(ok=True, amount=1000, streak=3, balance=5000)
    season = skins.active_season(XMAS_EVE)
    e = embeds.daily_embed(result, "18時", 0, season)
    assert "サンタ" in must(e.title) and "プレゼントが届いた" in must(e.description)
    assert "+1,000" in must(e.description)  # 額は変わらない
    plain = embeds.daily_embed(result, "18時", 0, skins.active_season(at(2027, 1, 1)))
    assert plain.title is not None and "デイリーボーナス" in plain.title and "サンタ" not in must(plain.description)


def test_a_season_must_end_after_it_starts_and_have_a_timezone():
    skin = SkinConfig(item_id="x", games=("チンチロ",), title_prefix="", start_line="", win_line="", lose_line="")
    with pytest.raises(ValueError):
        SeasonConfig(
            id="s", name="s", start="2026-12-26T00:00:00+09:00", end="2026-12-01T00:00:00+09:00", skins=(skin,)
        )
    with pytest.raises(ValueError):
        SeasonConfig(id="s", name="s", start="2026-12-01T00:00:00", end="2026-12-26T00:00:00", skins=(skin,))
