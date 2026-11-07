"""金額の入力欄: 自分の現在の残高を見せる（候補は残高の1行だけ）。"""

from types import SimpleNamespace

import pytest

from cogs._amount import balance_choices, typed_amount
from cogs.casino_cog import Casino
from cogs.economy_cog import EconomyCog
from cogs.loan_cog import LoanCog
from cogs.market_cog import MarketCog
from core.eco_types import INITIAL_BALANCE
from core.economy import Economy

USER = 7


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"))


def interaction(guild_id=1, **namespace):
    return SimpleNamespace(guild_id=guild_id, user=SimpleNamespace(id=USER), namespace=SimpleNamespace(**namespace))


def choices(eco, current="", minimum=100, maximum=10_000):
    bot = SimpleNamespace(economy=eco)
    return [(c.value, c.name) for c in balance_choices(bot, interaction(), current, minimum, maximum)]


def set_balance(eco, balance):
    eco.balance(USER)
    eco._conn.execute("UPDATE users SET balance=? WHERE user_id=?", (balance, USER))
    eco._conn.commit()


def test_typed_amount_reads_digits_commas_and_full_width():
    assert typed_amount("500") == 500 and typed_amount("1,200") == 1200 and typed_amount("１０００") == 1000
    assert (
        typed_amount("") is None
        and typed_amount("abc") is None
        and typed_amount("-5") is None
        and typed_amount("1.5") is None
    )


def test_empty_input_shows_only_the_balance(eco):
    set_balance(eco, 8_000)
    assert choices(eco) == [(100, "残高 8,000ペリカ（選ぶと 100 になります）")]  # 全額・半分などの候補は出さない


def test_typed_number_in_range_is_shown_with_the_balance(eco):
    set_balance(eco, 8_000)
    assert choices(eco, "500") == [(500, "500（残高 8,000ペリカ）")]
    assert choices(eco, "1,200") == [(1_200, "1,200（残高 8,000ペリカ）")]
    assert choices(eco, "１０００") == [(1_000, "1,000（残高 8,000ペリカ）")]


def test_typed_number_out_of_range_falls_back_to_the_balance_line(eco):
    set_balance(eco, 8_000)
    assert choices(eco, "50") == [(100, "残高 8,000ペリカ（選ぶと 100 になります）")]  # 最小未満
    assert choices(eco, "99999") == [(100, "残高 8,000ペリカ（選ぶと 100 になります）")]  # 上限超え
    assert choices(eco, "abc") == [(100, "残高 8,000ペリカ（選ぶと 100 になります）")]


def test_the_balance_is_shown_even_when_it_is_below_the_minimum_or_above_the_maximum(eco):
    set_balance(eco, 40)
    assert choices(eco) == [(100, "残高 40ペリカ（選ぶと 100 になります）")]
    set_balance(eco, 50_000)
    assert choices(eco, maximum=10_000) == [(100, "残高 50,000ペリカ（選ぶと 100 になります）")]


def test_new_player_sees_the_starting_balance_and_no_account_is_created(eco):
    found = choices(eco)
    assert f"残高 {INITIAL_BALANCE:,}ペリカ" in found[0][1]
    assert eco._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0  # 候補を出しただけでは、口座を作らない


def test_dm_has_no_balance_and_no_choices(eco):
    bot = SimpleNamespace(economy=eco)
    assert balance_choices(bot, interaction(guild_id=None), "", 100, 10_000) == []


def test_commands_use_the_balance_choices_on_their_amount_fields():
    wired = {
        "カジノ": ({"チンチロ", "パチンコ", "スロット", "ブラックジャック", "ルーレット"}, "bet", Casino.casino),
        "ペリカ": ({"送金"}, "amount", EconomyCog.group),
        "証券": ({"注文"}, "amount", MarketCog.group),
        "貸付": ({"提案"}, "principal", LoanCog.group),
    }
    for group_name, (names, param, group) in wired.items():
        found = {c.name: c for c in group.commands if c.name in names}
        assert set(found) == names, group_name
        for command in found.values():
            assert command._params[param].autocomplete, (group_name, command.name)  # 候補の登録がある
            option = next(
                o for o in command.to_dict(None)["options"] if o["name"] == command._params[param].display_name
            )
            assert (
                option["autocomplete"] is True and "choices" not in option
            )  # Discord は、候補の固定リストと併用できない
