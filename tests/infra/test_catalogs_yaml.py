"""catalogs/*.yaml と core/catalogs.py の境界バリデーション・互換型テスト。"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cogs.admin import embeds as _admin_embeds
from cogs.casino import pachinko as play_pachinko
from cogs.help import cog as help_cog
from cogs.labor import cog as labor_cog
from cogs.quiz import cog as quiz_cog
from cogs.raffle import cog as raffle_cog
from cogs.shared import ranking
from cogs.shop import embeds as _shop_embeds
from cogs.teiai import escape as _teiai_escape
from cogs.teiai import panel as _teiai_panel
from domain import (
    bonds,
    earnings,
    events,
    horse,
    labor,
    loans,
    market,
    market_events,
    missions,
    options,
    p2p,
    quizimport,
    quizpay,
    relics,
    shop,
    skins,
    teiai,
    weekly,
)
from domain import (
    types as eco_types,
)
from domain.games import blackjack, chinchiro, pachinko, roulette, rtplevel, slot
from infra import catalogs
from infra.db import economy


def test_all_yaml_catalogs_load_and_validate() -> None:
    assert catalogs.shop().insurance_cap == shop.INSURANCE_CAP == eco_types.INSURANCE_CAP
    assert len(catalogs.skins().skins) == len(skins.SKINS)
    assert len(catalogs.relics().buffs) > 0
    assert len(catalogs.bonds().kinds) == len(bonds.KINDS)
    assert len(catalogs.missions().universal) == len(missions.UNIVERSAL)
    assert len(catalogs.events().games) == len(events.GAMES)
    assert len(catalogs.labor().ores) == len(labor.ORE_VALUE)
    assert len(catalogs.horse().npc_names) == len(horse.NPC_NAMES)
    assert len(catalogs.market().market.symbols) == len(market.SYMBOLS)
    assert len(catalogs.teiai().statuses) == len(teiai.STATUS_LABEL)
    assert len(catalogs.economy().wallet.medals) == len(ranking.MEDALS)
    assert len(catalogs.games().slot.symbols) == len(slot.SYMBOLS)
    assert len(catalogs.help_pages().pages) == len(help_cog.PAGES)


def test_cross_catalog_consistency() -> None:
    assert len({i.id for i in shop.CATALOG}) == len(shop.CATALOG)
    assert shop.LABOR_DRINK in shop.STOCK_RULES
    assert p2p.SLOT_OF_KIND.items() <= _shop_embeds.SLOT_OF_KIND.items()
    assert _shop_embeds.EFFECT_OF_ITEM == eco_types.CONSUMABLE_EFFECTS
    assert skins.check_catalog() == []
    assert set(horse.BET_KINDS) == set(horse.MULTI_KINDS) | set(horse.COMBO_SIZE)
    assert labor.CLICK_GAP_MAX > 0 and labor.SUSPECT_HOURS > 0
    assert len(_teiai_escape.PROGRESS) > 0 and len(_teiai_panel.LEVEL_TEXT) == 4
    assert sum(p for _, _, p in pachinko.ROUNDS) == pytest.approx(1.0)
    assert 0.95 < pachinko.rtp() < 1.0
    assert missions.JACKPOT_ART.startswith("```") and missions.JACKPOT_ART.endswith("```")
    assert events.BOOST == pytest.approx(0.5)
    assert market.EVENT_PROB == pytest.approx(1 / 720)
    assert market_events.ANNOUNCE_PROB == pytest.approx(1 / 300)
    assert earnings.RELEASE_WEEKDAY == 1
    assert economy.Economy.DAILY_BASE > 0
    assert loans.USER_MIN_PRINCIPAL > 0
    assert quizpay.BASE_REWARD > 0
    assert quizimport.MAX_CHOICE_LEN == quiz_cog.MAX_CHOICE_LEN
    assert quizimport.DEFAULT_REWARD == quiz_cog.DEFAULT_REWARD
    assert raffle_cog.MIN_FEE > 0
    assert weekly.ANNOUNCE_TOP > 0
    assert p2p.FEE_RATE == pytest.approx(0.05)
    assert options.OPTION_HOURS > 0
    assert relics.BUFF_RATE > 0
    assert len(blackjack.SUITS) == 4 and len(blackjack.RANKS) == 13
    assert chinchiro.MAX_ROLLS == 3


def test_python_runtime_types_preserved() -> None:
    assert isinstance(roulette.RED, set)
    assert all(isinstance(n, int) for n in roulette.RED)
    assert isinstance(horse.FORM_MARK, dict)
    assert all(isinstance(k, int) for k in horse.FORM_MARK)
    assert isinstance(horse.COATS, list)
    assert isinstance(horse.SILKS, list)
    assert isinstance(horse.NPC_NAMES, list)
    assert isinstance(horse.BANNED_NAME_CHARS, set)
    assert isinstance(market.GAINS_TAX_BRACKETS, tuple)
    assert isinstance(ranking.MEDALS, dict)
    assert ranking.MEDALS == labor_cog.MEDALS
    assert all(isinstance(k, int) for k in ranking.MEDALS)
    assert isinstance(chinchiro.DICE_FACES, dict)
    assert all(isinstance(k, int) for k in chinchiro.DICE_FACES)
    assert isinstance(play_pachinko.BANNER, dict)
    assert all(isinstance(k, int) for k in play_pachinko.BANNER)
    assert isinstance(_admin_embeds.LEVEL_NAME, dict)
    assert all(isinstance(k, int) for k in _admin_embeds.LEVEL_NAME)
    assert isinstance(_shop_embeds.TABS, dict)
    assert all(isinstance(kinds, tuple) for _, kinds in _shop_embeds.TABS.values())
    assert isinstance(rtplevel.GAMES, tuple)
    assert isinstance(rtplevel.LEVELS, tuple)
    assert all(isinstance(title, str) and isinstance(lines, list) for title, lines in help_cog.PAGES.values())


def test_strict_model_rejects_unknown_keys(tmp_path: Path) -> None:
    bad_yaml = tmp_path / "shop.yaml"
    bad_yaml.write_text(
        (catalogs.CATALOG_DIR / "shop.yaml").read_text(encoding="utf-8") + "\nunknown_extra_key: 123\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        catalogs.load_yaml_model("shop.yaml", catalogs.ShopCatalog, root=tmp_path)


def test_shop_rejects_duplicate_item_id(tmp_path: Path) -> None:
    data = yaml.safe_load((catalogs.CATALOG_DIR / "shop.yaml").read_text(encoding="utf-8"))
    data["auto_used"] = []
    data["stock_rules"] = {}
    data["items"] = [
        {"id": "dup_item", "name": "A", "kind": "title", "price": 100},
        {"id": "dup_item", "name": "B", "kind": "title", "price": 200},
    ]
    (tmp_path / "shop.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValidationError, match="duplicate item id"):
        catalogs.load_yaml_model("shop.yaml", catalogs.ShopCatalog, root=tmp_path)


def test_pachinko_rejects_bad_probability_sum(tmp_path: Path) -> None:
    text = (catalogs.CATALOG_DIR / "games.yaml").read_text(encoding="utf-8")
    broken = text.replace("continue_prob: 0.50", "continue_prob: 0.50").replace("prob: 0.04", "prob: 0.50", 1)
    (tmp_path / "games.yaml").write_text(broken, encoding="utf-8")
    with pytest.raises(ValidationError, match="probabilities must sum to 1.0"):
        catalogs.load_yaml_model("games.yaml", catalogs.GamesCatalog, root=tmp_path)


def test_help_rejects_unknown_button_page(tmp_path: Path) -> None:
    (tmp_path / "help.yaml").write_text(
        """
home:
  title: "Home"
  description: "Desc"
  footer_template: "{min_bet:,}"
buttons:
  - key: missing_page
    label: "Missing"
pages:
  game:
    title: "Game"
    lines: ["Line 1"]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="unknown page"):
        catalogs.load_yaml_model("help.yaml", catalogs.HelpCatalog, root=tmp_path)


def test_missing_catalog_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        catalogs.load_yaml_model("does_not_exist.yaml", catalogs.ShopCatalog, root=tmp_path)
