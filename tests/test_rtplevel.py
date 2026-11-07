"""還元率レベル: 勝ち分だけの増減、レベルごとの還元率、保存、実際の精算への反映、コマンド。"""

import asyncio
import random
from types import SimpleNamespace

import pytest

from cogs.approval_cog import LEVEL_NAME, ApprovalCog, rtp_table_embed
from core.economy import INITIAL_BALANCE, Economy
from core.session import Sessions
from games import blackjack as bj
from games import chinchiro as ch
from games import pachinko, roulette, rtplevel, slot
from play import common, simple

OWNER, FRIEND = 1, 2


# ----------------------------- 計算 ----------------------------- #
def test_level_three_and_non_wins_are_untouched():
    for game in rtplevel.GAMES:
        assert rtplevel.adjust(game, 3, 100, 500) == 500
        for level in rtplevel.LEVELS:
            assert rtplevel.adjust(game, level, 100, 0) == 0  # 負け
            assert rtplevel.adjust(game, level, 100, 100) == 100  # 引き分け
            assert rtplevel.adjust(game, level, 100, 30) == 30  # 一部が戻るだけ（賭け金より少ない）


def test_higher_level_pays_more_and_lower_level_pays_less_on_a_win():
    for game in rtplevel.GAMES:
        paid = [rtplevel.adjust(game, lv, 1000, 3000) for lv in rtplevel.LEVELS]
        assert paid == sorted(paid) and len(set(paid)) == 6 and paid[2] == 3000
        assert paid[0] > 1000  # 最低のレベルでも、勝てば賭け金より多い


@pytest.mark.parametrize("game", ["スロット", "パチンコ"])
def test_exact_games_move_exactly_four_points_per_level(game):
    if game == "スロット":
        import itertools

        total = sum(slot.WEIGHTS.values())

        def rtp_at(level):
            r = 0.0
            for reels in itertools.product(slot.SYMBOLS, repeat=3):
                p = 1.0
                for s in reels:
                    p *= slot.WEIGHTS[s] / total
                m = slot.multiplier(reels)
                r += p * (1 + rtplevel.factor(game, level) * (m - 1) if m > 1 else m)
            return r
    else:

        def rtp_at(level):
            """セットの払い戻しの分布 [倍数] に、勝ち分の倍率をかけた還元率（セットごとに勝ち分へかける）。"""
            f = rtplevel.factor(game, level)
            return sum(p * (1 + f * (m - 1) if m > 1 else m) for m, p in enumerate(pachinko._set_distribution()))

    base = rtp_at(3)
    for level in rtplevel.LEVELS:
        assert abs(rtp_at(level) - (base + (level - 3) * rtplevel.STEP)) < 1e-9  # 1レベルごとにちょうど4ポイント


def test_roulette_uses_each_bet_kinds_own_factor():
    for kind in roulette.BETS:
        rtp, w = rtplevel.base_stats("ルーレット", kind)
        assert abs(rtp - roulette.rtp(kind)) < 1e-9
        mult = roulette.BETS[kind][1]
        p = rtp / mult
        for level in (1, 6):
            new_rtp = p * (1 + rtplevel.factor("ルーレット", level, kind) * (mult - 1))
            assert abs(new_rtp - (rtp + (level - 3) * rtplevel.STEP)) < 1e-9, kind  # どの賭け方でも4ポイントずつ


def test_simulated_constants_for_chinchiro_and_blackjack_are_close():
    rng = random.Random(5)
    n = 60_000
    tot, share = 0.0, 0.0
    for _ in range(n):
        o = ch.compare(ch.play_hand(rng)[-1][1], ch.play_hand(rng)[-1][1])
        pay, extra = ch.payout(1, o)
        t = pay - extra
        tot += t
        share += max(0, t - 1)
    assert abs(tot / n - rtplevel.CHINCHIRO_RTP) < 0.03 and abs(share / n - rtplevel.CHINCHIRO_W) < 0.03
    tot = share = 0.0
    for _ in range(n):
        g = bj.Game(rng)
        while not g.finished and bj.value(g.player) < 17:
            g.hit()
        if not g.finished:
            g.stand()
        r = bj.payout(g.result(), 1)
        tot += r
        share += max(0, r - 1)
    assert abs(tot / n - rtplevel.BLACKJACK_RTP) < 0.03 and abs(share / n - rtplevel.BLACKJACK_W) < 0.03


def test_approx_rtp_table_matches_the_proposal():
    assert round(rtplevel.approx_rtp("パチンコ", 1), 3) == round(pachinko.rtp() - 0.08, 3) == 0.89 and round(
        rtplevel.approx_rtp("パチンコ", 6), 3
    ) == round(pachinko.rtp() + 0.12, 3)
    assert round(rtplevel.approx_rtp("スロット", 3), 3) == round(slot.rtp(), 3)
    assert (
        rtplevel.approx_rtp("ルーレット", 4)
        > rtplevel.approx_rtp("ルーレット", 3)
        > rtplevel.approx_rtp("ルーレット", 2)
    )


# ----------------------------- 保存 ----------------------------- #
@pytest.mark.real_scope
def test_levels_are_saved_per_guild_and_game_with_three_as_default(tmp_path):
    eco = Economy(str(tmp_path / "t.db"))
    a, b = eco.for_guild(10), eco.for_guild(20)
    assert a.rtp_levels() == {g: 3 for g in rtplevel.GAMES}
    assert (
        a.rtp_level_set("スロット", 5) == ["スロット"]
        and a.rtp_level_get("スロット") == 5
        and a.rtp_level_get("パチンコ") == 3
    )
    assert b.rtp_level_get("スロット") == 3  # 別のサーバーは変わらない
    assert set(a.rtp_level_set(None, 2)) == set(rtplevel.GAMES) and set(a.rtp_levels().values()) == {2}
    a.rtp_level_set("スロット", 3)  # 3 に戻すと、保存はなくなる
    assert (
        a.rtp_level_get("スロット") == 3
        and eco._conn.execute("SELECT COUNT(*) FROM rtp_level WHERE game='スロット'").fetchone()[0] == 0
    )
    for bad in (0, 7):
        with pytest.raises(ValueError):
            a.rtp_level_set("スロット", bad)
    with pytest.raises(ValueError):
        a.rtp_level_set("コイン", 4)


# ----------------------------- 精算への反映 ----------------------------- #
def make_bot(tmp_path):
    return SimpleNamespace(
        economy=Economy(str(tmp_path / "t.db"), strict=False), sessions=Sessions(cooldown=0), config={}
    )


def play(bot, game, bet, payout, kind=None, level=3):
    eco = bot.economy
    eco.rtp_level_set(game, level)
    eid = eco.place_bet(1, bet, game)
    ctx = common.GameContext(bot, 1, eid, game, bet, 0)
    net = ctx.settle(payout, rtp_kind=kind)
    return ctx, net


def test_settlement_applies_the_level_only_to_wins_and_stays_consistent(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    monkeypatch.setattr(common, "local_now", lambda b: __import__("datetime").datetime(2026, 11, 6, 3, 0))
    ctx, net = play(bot, "パチンコ", 1000, 5000, level=6)  # 5倍に当たり
    assert net == rtplevel.adjust("パチンコ", 6, 1000, 5000) - 1000 and net > 4000 and ctx.rtp_shift == net - 4000
    ctx, net = play(bot, "パチンコ", 1000, 0, level=6)  # 負けは変わらない
    assert net == -1000 and ctx.rtp_shift == 0
    ctx, net = play(bot, "パチンコ", 1000, 5000, level=1)  # 渋いと、勝ち分が減る
    assert net < 4000 and ctx.rtp_shift < 0
    ctx, net = play(bot, "パチンコ", 1000, 5000, level=3)
    assert net == 4000 and ctx.rtp_shift == 0
    eco = bot.economy
    assert eco._conn.execute("SELECT SUM(delta) FROM ledger WHERE user_id=1").fetchone()[0] == eco.balance(1)


def test_roulette_passes_its_bet_kind_and_the_result_text_shows_the_adjusted_payout(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.economy.rtp_level_set("ルーレット", 6)
    monkeypatch.setattr(simple.roulette, "spin", lambda rng: 1)  # 赤の1
    monkeypatch.setattr(simple, "STEP", 0)
    sent, edits = [], []

    class It:
        user = SimpleNamespace(id=1, display_name="テスト")
        guild_id = 0
        response = SimpleNamespace(send_message=None)

        async def edit_original_response(self, **kw):
            edits.append(kw)

    it = It()

    async def send_message(*a, **kw):
        sent.append(kw)

    it.response = SimpleNamespace(send_message=send_message)
    asyncio.run(simple.play_roulette(it, bot, 1000, "red"))
    expected = rtplevel.adjust("ルーレット", 6, 1000, 2000, "red")
    assert expected > 2000
    desc = edits[-1]["embed"].description
    assert f"払い戻し: **{expected:,}ペリカ**" in desc and f"{expected - 1000:+,}" in edits[-1]["embed"].title
    assert bot.economy.balance(1) == INITIAL_BALANCE - 1000 + expected


# ----------------------------- コマンド ----------------------------- #
def make_admin(tmp_path, monkeypatch):
    eco = Economy(str(tmp_path / "t.db"))
    guild = SimpleNamespace(id=5, name="友達鯖", members=[])
    monkeypatch.setattr("cogs.approval_cog.approval.is_allowed", lambda *a: True)
    bot = SimpleNamespace(
        config={"owner_ids": [OWNER]},
        economy=eco,
        guilds=[guild],
        get_guild=lambda gid: guild if gid == 5 else None,
        get_cog=lambda n: None,
    )
    return ApprovalCog(bot), eco


def inter(uid, log):
    class R:
        async def send_message(self, **kw):
            log.append(kw["embeds"] if "embeds" in kw else [kw["embed"]])

    return SimpleNamespace(user=SimpleNamespace(id=uid), guild_id=None, response=R(), namespace=SimpleNamespace())


@pytest.mark.real_scope
def test_commands_are_owner_only_and_change_the_chosen_game(tmp_path, monkeypatch):
    cog, eco = make_admin(tmp_path, monkeypatch)
    log = []
    pick = lambda g, n: SimpleNamespace(value=g, name=n)  # noqa: E731
    asyncio.run(cog.rtp_set.callback(cog, inter(FRIEND, log), "5", pick("スロット", "スロット"), pick(6, "6")))
    asyncio.run(cog.rtp_list.callback(cog, inter(FRIEND, log), None))
    assert [e[0].title for e in log] == ["❌ 権限がありません"] * 2 and eco.for_guild(5).rtp_level_get("スロット") == 3
    log.clear()
    asyncio.run(cog.rtp_set.callback(cog, inter(OWNER, log), "5", pick("スロット", "スロット"), pick(6, "6")))
    assert eco.for_guild(5).rtp_level_get("スロット") == 6 and log[-1][0].title == "✅ 還元率のレベルを変えました"
    assert (
        "**スロット**　レベル **6**（大盤振る舞い）" in log[-1][1].description
        and "標準から変更中" in log[-1][1].description
    )
    asyncio.run(cog.rtp_set.callback(cog, inter(OWNER, log), "5", pick("*", "すべて"), pick(2, "2")))
    assert set(eco.for_guild(5).rtp_levels().values()) == {2}
    asyncio.run(cog.rtp_list.callback(cog, inter(OWNER, log), "5"))
    assert log[-1][0].title == "🎚️ 友達鯖 の還元率" and log[-1][0].description.count("レベル **2**") == 5
    asyncio.run(cog.rtp_set.callback(cog, inter(OWNER, log), "999", pick("*", "すべて"), pick(2, "2")))
    assert log[-1][0].title == "❌ サーバーが見つかりません"


def test_table_embed_marks_changed_games_and_names_every_level():
    e = rtp_table_embed({g: 3 for g in rtplevel.GAMES}, "t")
    assert "標準から変更中" not in e.description and e.description.count("標準") >= 5
    changed = {**{g: 3 for g in rtplevel.GAMES}, "パチンコ": 1}
    assert (
        "パチンコ**　レベル **1**（かなり渋い）　還元率 約89.0%　⚠️ 標準から変更中"
        in rtp_table_embed(changed, "t").description
    )
    assert set(LEVEL_NAME) == set(rtplevel.LEVELS)


def test_pachinko_sets_apply_the_level_to_each_set_not_to_the_sum(tmp_path, monkeypatch):
    """連続投入では、勝ったセットの勝ち分だけを増減する（負けたセットの分と相殺しない）。"""
    from games import pachinko as pg
    from play import pachinko as play_pa

    bot = make_bot(tmp_path)
    bot.economy.rtp_level_set("パチンコ", 6)
    bot.economy.credit(1, 100_000, "テスト")
    hit = pg.SpinInfo("", "", True, (10,), 0.1)
    miss = pg.SpinInfo("", "", False, (), 0.01)
    plan = iter(
        [
            pg.SetResult(3, (hit, miss, miss), 10),
            pg.SetResult(3, (miss,) * 3, 0),
            pg.SetResult(3, (miss,) * 3, 0),
        ]
    )  # 10倍が1回、あとは2回ハズレ
    monkeypatch.setattr(pg, "roll_set", lambda rng: next(plan))
    monkeypatch.setattr(play_pa, "STEP", 0)
    monkeypatch.setattr(common, "local_now", lambda b: __import__("datetime").datetime(2026, 11, 6, 3, 0))
    sent, edits = [], []

    class It:
        user = SimpleNamespace(id=1, display_name="テスト")
        guild_id = 0
        response = None

        async def edit_original_response(self, **kw):
            edits.append(kw)

    it = It()

    async def send_message(*a, **kw):
        sent.append(kw)

    it.response = SimpleNamespace(send_message=send_message)
    before = bot.economy.balance(1)
    asyncio.run(play_pa.play_pachinko(it, bot, 1000, 3))
    expected = rtplevel.adjust("パチンコ", 6, 1000, 10_000)  # 勝った1セットだけ増える
    assert expected > 10_000
    assert bot.economy.balance(1) == before - 3000 + expected
    assert f"払い戻し: **{expected:,}ペリカ**" in edits[-1]["embed"].description
