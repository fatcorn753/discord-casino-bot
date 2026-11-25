"""金額の入力欄: 自分の現在の残高を見せる（候補は残高の1行だけ）。"""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from discord import app_commands

from cogs.casino.cog import Casino
from cogs.economy.cog import EconomyCog
from cogs.loan.cog import LoanCog
from cogs.market.cog import MarketCog
from cogs.shared.amount import balance_choices, typed_amount
from domain.types import INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import as_interaction
from tests.support import make_test_bot

USER = 7


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"))


def interaction(guild_id: int | None = 1, **namespace):
    return SimpleNamespace(guild_id=guild_id, user=SimpleNamespace(id=USER), namespace=SimpleNamespace(**namespace))


def economy(eco):
    return make_test_bot(repos=eco).services.economy


def choices(eco, current="", minimum=100, maximum=10_000):
    return [
        (c.value, c.name)
        for c in balance_choices(economy(eco), as_interaction(interaction()), current, minimum, maximum)
    ]


def set_balance(eco, balance):
    eco.wallet.balance(USER)
    with eco.db.tx():
        eco.db.conn.execute("UPDATE accounts SET balance=? WHERE user_id=?", (balance, USER))


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
    assert eco.db.conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0  # 候補を出しただけでは、口座を作らない


def test_dm_has_no_balance_and_no_choices(eco):
    assert balance_choices(economy(eco), as_interaction(interaction(guild_id=None)), "", 100, 10_000) == []


def test_commands_use_the_balance_choices_on_their_amount_fields():
    wired = {
        "カジノ": ({"チンチロ", "パチンコ", "スロット", "ブラックジャック", "ルーレット"}, "bet", Casino.casino),
        "ペリカ": ({"送金"}, "amount", EconomyCog.group),
        "証券 注文": ({"現物", "信用", "コール", "プット", "ペア", "裸売り", "債券"}, "amount", MarketCog.order_group),
        "貸付": ({"提案"}, "principal", LoanCog.group),
    }
    for group_name, (names, param, group) in wired.items():
        found = {c.name: c for c in group.commands if c.name in names and isinstance(c, app_commands.Command)}
        assert set(found) == names, group_name
        for command in found.values():
            assert command._params[param].autocomplete, (group_name, command.name)  # 候補の登録がある
            option = next(
                o
                for o in command.to_dict(cast("app_commands.CommandTree[Any]", None))["options"]
                if o["name"] == command._params[param].display_name
            )
            assert (
                option["autocomplete"] is True and "choices" not in option
            )  # Discord は、候補の固定リストと併用できない


def test_optional_amount_tells_blank_from_bad_input():
    from cogs.shared.amount import optional_amount

    assert optional_amount("") == (True, None) and optional_amount("  ") == (True, None)
    assert optional_amount("１，０００") == (True, 1000) and optional_amount("2,500") == (True, 2500)
    assert optional_amount("abc") == (False, None) and optional_amount("-5") == (False, None)


class _Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, *a, **kw):
        self.sent.append(kw)


def test_amount_modal_passes_the_amount_or_rejects_bad_input():
    import asyncio

    from cogs.shared.amount import AmountModal

    got = []

    async def on_amount(it, amount):
        got.append(amount)

    modal = AmountModal(title="t", label="金額", required=False, on_amount=on_amount)
    it = SimpleNamespace(response=_Resp())
    for raw in ("1,200", "", "１０"):
        modal.amount._value = raw
        asyncio.run(modal.on_submit(as_interaction(it)))
    assert got == [1200, None, 10] and it.response.sent == []
    modal.amount._value = "十"
    asyncio.run(modal.on_submit(as_interaction(it)))
    assert got == [1200, None, 10] and "金額が正しくありません" in it.response.sent[0]["embed"].title
