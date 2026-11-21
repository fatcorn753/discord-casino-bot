"""共有ジャックポットと、チンチロの卓。"""

import asyncio
import random
from types import SimpleNamespace

import pytest

from cogs.casino import chinchiro_table, common
from domain.games import chinchiro as rules
from domain.games import jackpot
from domain.types import INITIAL_BALANCE
from tests.fakes import as_interaction, must
from tests.support import make_casino_deps, make_test_bot

G = 10


@pytest.fixture
def bot(tmp_path):
    b = make_test_bot(str(tmp_path / "t.db"))
    yield b
    b.repos.close()


def run(coro):
    return asyncio.run(coro)


def pool(bot) -> int:
    return bot.repos.for_guild(G).escrow.jackpot_pool()


def play(bot, uid: int, bet: int, payout: int) -> common.GameContext:
    deps = make_casino_deps(bot)
    escrow = must(bot.repos.for_guild(G).escrow.place_bet(uid, bet, "スロット"))
    ctx = common.GameContext(deps, uid, escrow, "スロット", bet, G, f"u{uid}")
    ctx.settle(payout)
    return ctx


# ----------------------------- 共有ジャックポット ----------------------------- #
def test_losses_feed_the_pool_without_changing_the_bank_share(bot):
    bot.repos.for_guild(G).wallet.balance(1)
    play(bot, 1, 2_000, 0)  # 2千負け
    assert pool(bot) == int(2_000 * jackpot.SHARE)
    play(bot, 1, 1_000, 3_000)  # 勝ちは積まない
    assert pool(bot) == int(2_000 * jackpot.SHARE)


def test_chance_grows_with_the_bet_but_is_capped_and_needs_a_minimum_pool():
    assert jackpot.chance(500, jackpot.MIN_POOL) == 500 / jackpot.PELICA_PER_CERTAIN
    assert jackpot.chance(10**9, jackpot.MIN_POOL) == jackpot.MAX_CHANCE
    assert jackpot.chance(500, jackpot.MIN_POOL - 1) == 0.0
    assert jackpot.chance(0, 10**9) == 0.0
    rng = random.Random(1)
    assert not any(jackpot.draw(rng, 500, jackpot.MIN_POOL - 1) for _ in range(1000))


def test_a_hit_pays_the_whole_pool_once_and_resets_it(bot, monkeypatch):
    eco = bot.repos.for_guild(G)
    for uid in (1, 2):
        eco.wallet.balance(uid)
    with eco.db.tx():
        eco.db.conn.execute("INSERT INTO jackpot (guild_id, pool) VALUES (?, 50000)", (eco.escrow.guild_id,))
    monkeypatch.setattr(jackpot, "draw", lambda rng, bet, pool: True)
    ctx = play(bot, 2, 1_000, 0)
    assert ctx.jackpot_won == 50_000 + int(1_000 * jackpot.SHARE) and ctx.jackpot_pool == 0
    assert eco.wallet.balance(2) == INITIAL_BALANCE - 1_000 + ctx.jackpot_won
    assert "共有ジャックポット" in common.result_lines(ctx, 0, ctx.jackpot_won - 1_000)
    assert eco.escrow.jackpot_claim(1) == 0  # 空の積み立ては払わない


def test_result_shows_the_current_pool(bot):
    bot.repos.for_guild(G).wallet.balance(1)
    ctx = play(bot, 1, 2_000, 0)
    assert f"いま **{int(2_000 * jackpot.SHARE):,}" in common.result_lines(ctx, 0, -2_000)


# ----------------------------- チンチロの卓 ----------------------------- #
class Message:
    def __init__(self):
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


class Response:
    def __init__(self):
        self.sent, self.edited = [], []

    def is_done(self):
        return bool(self.sent or self.edited)

    async def send_message(self, *args, **kw):
        self.sent.append((args, kw))

    async def edit_message(self, **kw):
        self.edited.append(kw)

    async def defer(self):
        pass


def interaction(bot, uid, msg):
    async def original_response():
        return msg

    return SimpleNamespace(
        guild_id=G,
        user=SimpleNamespace(id=uid, display_name=f"u{uid}"),
        client=bot,
        response=Response(),
        followup=SimpleNamespace(),
        original_response=original_response,
    )


async def no_sleep(_):
    return None


def test_table_seats_several_players_against_one_dealer(bot, monkeypatch):
    monkeypatch.setattr(chinchiro_table.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(chinchiro_table, "JOIN_SECONDS", 3600)  # 締切のタスクは走らせない
    deps, msg = make_casino_deps(bot), Message()
    eco = bot.repos.for_guild(G)

    async def scenario():
        table = must(await chinchiro_table.open_table(as_interaction(interaction(bot, 1, msg)), deps, 1_000))
        it2 = interaction(bot, 2, msg)
        assert await table.sit(as_interaction(it2)) is None
        assert await table.sit(as_interaction(interaction(bot, 2, msg))) == "もう座っています。"
        assert await table.sit(as_interaction(interaction(bot, 3, msg))) is None
        assert await table.stand(3)  # 始まる前は立てる（全額戻る）
        await table.play()
        await table.play()  # 二度目は何もしない
        if table._task:
            table._task.cancel()
        return table

    # 親は必ず目なし（子は誰でも、目なし同士でなければ勝つか分ける）にして、精算の筋だけ確かめる
    real = rules.play_hand
    calls = iter([[((1, 2, 4), rules.Hand(rules.MENASHI))] * 3])
    monkeypatch.setattr(rules, "play_hand", lambda rng: next(calls, None) or real(rng))
    table = run(scenario())
    assert table.started and set(table.seats) == {1, 2} and all(c.done for c in table.seats.values())
    assert eco.wallet.balance(3) == INITIAL_BALANCE  # 立った人は、賭け金が戻っている
    assert eco.db.conn.execute("SELECT COUNT(*) FROM escrow").fetchone()[0] == 0  # 預かり金は残らない
    final = msg.edits[-1]["embed"]
    assert "決着" in must(final.title) and "u1" in must(final.description) and "u2" in must(final.description)


def test_empty_table_folds(bot, monkeypatch):
    monkeypatch.setattr(chinchiro_table, "JOIN_SECONDS", 3600)
    deps, msg = make_casino_deps(bot), Message()

    async def scenario():
        table = must(await chinchiro_table.open_table(as_interaction(interaction(bot, 1, msg)), deps, 1_000))
        assert await table.stand(1)
        await table.play()
        if table._task:
            table._task.cancel()

    run(scenario())
    assert "たたみました" in must(msg.edits[-1]["embed"].description)
    assert bot.repos.for_guild(G).wallet.balance(1) == INITIAL_BALANCE
