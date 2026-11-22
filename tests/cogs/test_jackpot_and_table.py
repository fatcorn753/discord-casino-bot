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


def hand(kind, value=0):
    return [((1, 1, 1), rules.Hand(kind, value))]


def test_pot_payouts_go_to_the_strongest_hand_minus_the_rake():
    bet = 1_000
    p = rules.pot_payouts({1: rules.Hand(rules.SHIGORO), 2: rules.Hand(rules.ME, 3), 3: rules.Hand(rules.HIFUMI)}, bet)
    assert p == {1: bet + int(2 * bet * (1 - rules.TABLE_RAKE)), 2: 0, 3: 0}
    tie = rules.pot_payouts({1: rules.Hand(rules.ME, 5), 2: rules.Hand(rules.ME, 5), 3: rules.Hand(rules.MENASHI)}, bet)
    assert tie[1] == tie[2] == bet + int(bet * (1 - rules.TABLE_RAKE)) // 2 and tie[3] == 0
    even = rules.pot_payouts({1: rules.Hand(rules.ME, 2), 2: rules.Hand(rules.ME, 2)}, bet)
    assert even == {1: bet, 2: bet}  # 全員が同じ強さなら、全額戻る
    # 払い戻しの合計は、出した額を超えない（ペリカを作らない）
    assert sum(p.values()) <= 3 * bet and sum(tie.values()) <= 3 * bet


def patch_table(monkeypatch, hands):
    monkeypatch.setattr(chinchiro_table, "JOIN_SECONDS", 3600)  # 締切のタスクは走らせない
    monkeypatch.setattr(chinchiro_table, "ROLL_SECONDS", 3600)
    it = iter(hands)
    monkeypatch.setattr(rules, "play_hand", lambda rng: next(it))


def cancel(table):
    for t in (table._task, table._roll_task):
        if t:
            t.cancel()


def test_table_pot_goes_to_the_strongest_roller(bot, monkeypatch):
    # 座る順: 1, 2（3 は立つ）。振る順: 2 が嵐、1 が目。2 が総取りする
    patch_table(monkeypatch, [hand(rules.ARASHI, 4), hand(rules.ME, 6)])
    deps, msg = make_casino_deps(bot), Message()
    eco = bot.repos.for_guild(G)

    async def scenario():
        table = must(await chinchiro_table.open_table(as_interaction(interaction(bot, 1, msg)), deps, 1_000))
        assert await table.sit(as_interaction(interaction(bot, 2, msg))) is None
        assert await table.sit(as_interaction(interaction(bot, 2, msg))) == "もう座っています。"
        assert await table.sit(as_interaction(interaction(bot, 3, msg))) is None
        assert await table.stand(3)  # 始まる前は立てる（全額戻る）
        assert await table.roll(1) == "まだ始まっていません。"
        await table.play()
        await table.play()  # 二度目は何もしない
        assert await table.sit(as_interaction(interaction(bot, 4, msg))) == "もう始まっています。"
        assert await table.roll(4) == "この卓に座っていません。"
        assert await table.roll(2) is None
        assert await table.roll(2) == "もう振りました。"
        assert not table.all_rolled
        assert await table.roll(1) is None
        assert table.all_rolled
        await table.finish()
        await table.finish()  # 二度目は何もしない
        cancel(table)
        return table

    table = run(scenario())
    assert all(c.done for c in table.seats.values())
    win = int(1_000 * (1 - rules.TABLE_RAKE))
    assert eco.wallet.balance(2) == INITIAL_BALANCE + win
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 1_000
    assert eco.wallet.balance(3) == INITIAL_BALANCE
    assert eco.db.conn.execute("SELECT COUNT(*) FROM escrow").fetchone()[0] == 0  # 預かり金は残らない
    final = msg.edits[-1]["embed"]
    assert "決着" in must(final.title) and "u2** の総取り" in must(final.description)


def test_finish_auto_rolls_those_who_did_not_roll(bot, monkeypatch):
    patch_table(monkeypatch, [hand(rules.ME, 2), hand(rules.ME, 2)])
    deps, msg = make_casino_deps(bot), Message()
    eco = bot.repos.for_guild(G)

    async def scenario():
        table = must(await chinchiro_table.open_table(as_interaction(interaction(bot, 1, msg)), deps, 1_000))
        assert await table.sit(as_interaction(interaction(bot, 2, msg))) is None
        await table.play()
        assert await table.roll(1) is None
        await table.finish()  # 時間切れ: 2 は自動で振る
        cancel(table)
        return table

    table = run(scenario())
    assert set(table.rolls) == {1, 2}
    assert eco.wallet.balance(1) == eco.wallet.balance(2) == INITIAL_BALANCE  # 同じ強さなので全額戻る
    assert "引き分け" in must(msg.edits[-1]["embed"].description)


def test_one_player_table_folds_and_refunds(bot, monkeypatch):
    patch_table(monkeypatch, [])
    deps, msg = make_casino_deps(bot), Message()

    async def scenario():
        table = must(await chinchiro_table.open_table(as_interaction(interaction(bot, 1, msg)), deps, 1_000))
        await table.play()
        assert await table.roll(1) == "もう決着しています。"
        cancel(table)

    run(scenario())
    assert "たたみました" in must(msg.edits[-1]["embed"].description)
    assert bot.repos.for_guild(G).wallet.balance(1) == INITIAL_BALANCE


def test_empty_table_folds(bot, monkeypatch):
    patch_table(monkeypatch, [])
    deps, msg = make_casino_deps(bot), Message()

    async def scenario():
        table = must(await chinchiro_table.open_table(as_interaction(interaction(bot, 1, msg)), deps, 1_000))
        assert await table.stand(1)
        await table.play()
        cancel(table)

    run(scenario())
    assert "たたみました" in must(msg.edits[-1]["embed"].description)
    assert bot.repos.for_guild(G).wallet.balance(1) == INITIAL_BALANCE
