"""Discord 側のゲームの流れ（賭け金の預かり・精算・中断時の返金）を、偽の Interaction で確かめる。"""

import asyncio
import os
import random
from types import SimpleNamespace

import pytest

from core import teiai
from core.economy import INITIAL_BALANCE, Economy
from core.session import Sessions
from games import blackjack as bj
from games import chinchiro as ch
from play import blackjack as play_bj
from play import chinchiro as play_ch
from play import common, simple
from play import pachinko as play_pa


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def nop(*a, **k):
        return None

    monkeypatch.setattr(asyncio, "sleep", nop)


@pytest.fixture
def bot(tmp_path):
    return SimpleNamespace(economy=Economy(str(tmp_path / "t.db")), sessions=Sessions(cooldown=0), config={})


class FakeInteraction:
    def __init__(self, uid=1, fail_edit_after=None, fail_with=None):
        self.user = SimpleNamespace(id=uid, display_name="テスト")
        self.guild_id = 10
        self.sent, self.edits = [], []
        self.fail_edit_after = fail_edit_after
        self.fail_with = fail_with or RuntimeError("編集に失敗")
        self.response = SimpleNamespace(send_message=self._send, edit_message=self._edit_msg)

    async def _send(self, *args, **kw):
        if args:
            kw["content"] = args[0]
        self.sent.append(kw)

    async def _edit_msg(self, **kw):
        self.edits.append(kw)

    async def edit_original_response(self, **kw):
        if self.fail_edit_after is not None and len(self.edits) >= self.fail_edit_after:
            raise self.fail_with
        self.edits.append(kw)


def run(coro):
    return asyncio.run(coro)


def title_of(interaction):
    return (interaction.edits[-1] if interaction.edits else interaction.sent[-1])["embed"].title


def assert_clean(bot, uid=1):
    """ゲーム後に、預かり金が残っておらず、次のゲームを始められる。"""
    assert bot.economy._conn.execute("SELECT COUNT(*) FROM escrow").fetchone()[0] == 0
    assert not bot.sessions.is_active(uid)


def ledger_matches_balance(bot, uid=1):
    total = bot.economy._conn.execute("SELECT SUM(delta) FROM ledger WHERE user_id=?", (uid,)).fetchone()[0]
    return total == bot.economy.balance(uid)


# ---------------------------- 賭け金の検証 ---------------------------- #
def test_bet_below_the_minimum_and_insufficient_are_rejected_without_charging(bot):
    it = FakeInteraction()
    assert run(common.start_game(it, bot, 1, "テスト")) is None
    assert it.sent[0]["ephemeral"] and "少なすぎ" in it.sent[0]["embed"].title
    it = FakeInteraction()
    assert run(common.start_game(it, bot, INITIAL_BALANCE + 1, "テスト")) is None
    assert "足りません" in it.sent[0]["embed"].title
    assert bot.economy.balance(1) == INITIAL_BALANCE
    assert_clean(bot)


def test_cannot_start_second_game_while_playing(bot):
    ctx = run(common.start_game(FakeInteraction(), bot, 100, "A"))
    assert ctx is not None
    it = FakeInteraction()
    assert run(common.start_game(it, bot, 100, "B")) is None
    assert "遊んでいる" in it.sent[0]["embed"].description
    assert bot.economy.balance(1) == INITIAL_BALANCE - 100  # 2つ目の賭け金は引かれていない
    ctx.settle(0)


# ------------------------- 結果が出るゲーム ------------------------- #
@pytest.mark.parametrize("play", ["slot", "roulette"])
def test_instant_games_settle_and_keep_ledger_consistent(bot, monkeypatch, play):
    monkeypatch.setattr(simple, "RNG", random.Random(5))
    for i in range(30):
        it = FakeInteraction()
        if play == "slot":
            run(simple.play_slot(it, bot, 100))
        else:
            run(simple.play_roulette(it, bot, 100, "red"))
        assert_clean(bot)
        assert bot.economy.balance(1) >= 0
    assert ledger_matches_balance(bot)


def test_roulette_red_win_pays_double(bot, monkeypatch):
    monkeypatch.setattr(simple.roulette, "spin", lambda rng: 1)  # 出目は必ず赤の1
    it = FakeInteraction()
    run(simple.play_roulette(it, bot, 1000, "red"))
    assert bot.economy.balance(1) == INITIAL_BALANCE - 1000 + 2000


def test_roulette_validates_number_before_charging(bot):
    it = FakeInteraction()
    run(simple.play_roulette(it, bot, 100, "number", None))
    assert "数字が必要" in it.sent[0]["embed"].title and bot.economy.balance(1) == INITIAL_BALANCE
    it = FakeInteraction()
    run(simple.play_roulette(it, bot, 100, "red", 7))
    assert "数字は不要" in it.sent[0]["embed"].title and bot.economy.balance(1) == INITIAL_BALANCE


def http_error(status):
    import discord

    cls = discord.NotFound if status == 404 else discord.HTTPException  # 実際の Discord も 404 は NotFound になる
    return cls(SimpleNamespace(status=status, reason="x"), "err")


def test_display_failure_after_result_does_not_change_the_result(bot, monkeypatch):
    """演出の途中でメッセージが消えても、結果は先に精算済み（外れが返金に変わらない）。"""
    monkeypatch.setattr(simple.roulette, "spin", lambda rng: 2)  # 赤に賭けたが、出目は黒の2＝ハズレ
    it = FakeInteraction(fail_edit_after=1, fail_with=http_error(404))
    run(simple.play_roulette(it, bot, 500, "red"))
    assert bot.economy.balance(1) == INITIAL_BALANCE - 500
    assert_clean(bot)


def test_non_display_error_after_settle_keeps_result(bot, monkeypatch):
    monkeypatch.setattr(simple, "RNG", random.Random(1))
    it = FakeInteraction(fail_edit_after=1)  # 想定外の例外（バグ）
    before = bot.economy.balance(1)
    with pytest.raises(RuntimeError):
        run(simple.play_slot(it, bot, 500))
    after = bot.economy.balance(1)
    reels = simple.slot.spin(random.Random(1))
    assert after == before - 500 + int(500 * simple.slot.multiplier(reels))  # 返金ではなく、精算した結果のまま
    assert_clean(bot)


def test_error_before_settle_refunds_bet(bot, monkeypatch):
    def boom(rng):
        raise ValueError("バグ")

    monkeypatch.setattr(simple.slot, "spin", boom)
    with pytest.raises(ValueError):
        run(simple.play_slot(FakeInteraction(), bot, 500))
    assert bot.economy.balance(1) == INITIAL_BALANCE  # 精算の前に失敗したら、賭け金は返る
    assert bot.economy.stats(1)["games_played"] == 0  # 返金は成績に数えない
    assert_clean(bot)


def test_pachinko_plays_all_frames_and_settles(bot, monkeypatch):
    monkeypatch.setattr(play_pa, "RNG", random.Random(2))
    monkeypatch.setattr(play_pa, "STEP", 0)
    bot.economy.credit(1, 100_000, "テスト")  # 40回遊ぶので、残高を増やしておく
    for _ in range(40):
        it = FakeInteraction()
        run(play_pa.play_pachinko(it, bot, 100))
        assert len(it.edits) >= 4 and "パチンコ" in title_of(it)
        assert_clean(bot)
    assert ledger_matches_balance(bot)
    stats = bot.economy.pachinko_stats()
    assert stats["sets"][0] == 40 and stats["spins"][0] == stats["sets"][5]  # セットと回転ごとに、統計用の記録が残る
    assert sum(n for n, _ in stats["omen"].values()) == stats["spins"][0]


# ------------------------------ チンチロ ------------------------------ #
def make_chinchiro_view(bot, bet, dealer_dice, player_dice_list):
    ctx = run(common.start_game(FakeInteraction(), bot, bet, "チンチロ"))
    dealer_rolls = [(dealer_dice, ch.classify(dealer_dice))]
    view = play_ch.ChinchiroView(ctx, FakeInteraction(), dealer_rolls)
    view.player_rolls = [(d, ch.classify(d)) for d in player_dice_list]
    return ctx, view


def test_chinchiro_win_pays_by_role(bot):
    ctx, view = make_chinchiro_view(bot, 1000, (1, 1, 3), [(1, 1, 1)])  # 子がピンゾロ（5倍）
    view.final_embed()
    assert bot.economy.balance(1) == INITIAL_BALANCE + 5000
    assert_clean(bot)


def test_chinchiro_skins_change_only_the_look_not_the_payout(bot):
    from core import skins

    results = {}
    for name in (None, "skin_gold_dice", "skin_kuro"):
        bot.economy.credit(1, 100_000, "テスト")
        before = bot.economy.balance(1)
        ctx = run(common.start_game(FakeInteraction(), bot, 1000, "チンチロ"))
        ctx.skin = skins.SKINS.get(name)
        view = play_ch.ChinchiroView(ctx, FakeInteraction(), [((1, 1, 3), ch.classify((1, 1, 3)))])
        view.player_rolls = [((1, 1, 1), ch.classify((1, 1, 1)))]  # 子がピンゾロ（5倍）
        embed = view.final_embed()
        results[name] = (bot.economy.balance(1) - before, embed.description)
    assert results[None][0] == results["skin_gold_dice"][0] == results["skin_kuro"][0] == 5000  # 配当は同じ
    plain, gold, kuro = (results[k][1] for k in (None, "skin_gold_dice", "skin_kuro"))
    assert "🟡⚀ 🟡⚀ 🟡⚀" in gold and "🟡" not in plain  # サイコロの飾り
    assert skins.SKINS["skin_gold_dice"].role_win["pinzoro"] in gold and "黄金のピンゾロ" not in plain  # 役ごとの地の文
    assert "黒服「ピンゾロ…。5倍" in kuro and "⚫⚀ ⚫⚀ ⚫⚀" in kuro and "黒服「…ピンゾロ」" in kuro  # 実況と、役の文


def test_chinchiro_loss_beyond_balance_becomes_debt(bot):
    # 全財産を賭けて、親のピンゾロ（5倍払い）で負ける: 残高は0、払い切れない分は借金になる
    ctx, view = make_chinchiro_view(bot, INITIAL_BALANCE, (1, 1, 1), [(2, 2, 3)])
    embed = view.final_embed()
    assert (
        bot.economy.balance(1) == teiai.SEED and bot.economy.debt(1) == INITIAL_BALANCE * 4 + teiai.SEED
    )  # 手元が空なので、種銭
    assert "借金" in embed.description and "種銭" in embed.description
    assert_clean(bot) and ledger_matches_balance(bot)


def test_chinchiro_lose_double_extra_comes_from_remaining_balance(bot):
    bot.economy.credit(1, 7000, "テスト")  # 残高 10,000
    ctx, view = make_chinchiro_view(bot, 1000, (1, 1, 1), [(2, 2, 3)])  # 親ピンゾロ: 5倍 = 賭け金1000 + 追加4000
    view.final_embed()
    assert bot.economy.balance(1) == 10_000 - 1000 - 4000
    assert ledger_matches_balance(bot)


def test_chinchiro_draw_returns_bet(bot):
    ctx, view = make_chinchiro_view(bot, 500, (2, 2, 4), [(4, 2, 2)])
    view.final_embed()
    assert bot.economy.balance(1) == INITIAL_BALANCE


def test_chinchiro_timeout_settles_as_menashi_loss(bot):
    ctx, view = make_chinchiro_view(bot, 400, (2, 2, 4), [])  # 放置
    run(view.on_timeout())
    assert bot.economy.balance(1) == INITIAL_BALANCE - 400  # 目なし扱いで負け。賭け金は預かったままにならない
    assert_clean(bot)


def test_chinchiro_full_flow_with_button(bot, monkeypatch):
    monkeypatch.setattr(play_ch, "RNG", random.Random(4))
    it = FakeInteraction()
    run(play_ch.play_chinchiro(it, bot, 300))
    view = it.edits[-1]["view"]
    assert view is not None and not bot.economy._conn.execute("SELECT COUNT(*) FROM escrow").fetchone()[0] == 0
    for _ in range(3):  # 押し続ければ必ず終わる（最大3回）
        if view.ctx.done:
            break
        press = FakeInteraction()
        run(view.roll.callback(press))
    assert view.ctx.done
    assert_clean(bot) and ledger_matches_balance(bot)


# ---------------------------- ブラックジャック ---------------------------- #
def make_bj(bot, bet, player, dealer, deck=()):
    ctx = run(common.start_game(FakeInteraction(), bot, bet, "ブラックジャック"))
    game = bj.Game(random.Random(0))
    c = lambda *ranks: [(r, "♠") for r in ranks]  # noqa: E731
    game.player, game.dealer, game.finished, game.deck = c(*player), c(*dealer), False, c(*deck)
    return ctx, play_bj.BlackjackView(ctx, FakeInteraction(), game)


def test_blackjack_stand_win_and_natural(bot):
    ctx, view = make_bj(bot, 1000, ("10", "9"), ("10", "7"))
    view.game.stand()
    view.final_embed()
    assert bot.economy.balance(1) == INITIAL_BALANCE + 1000
    ctx, view = make_bj(bot, 1000, ("A", "K"), ("10", "7"))
    view.game.stand()
    view.final_embed()
    assert bot.economy.balance(1) == INITIAL_BALANCE + 1000 + 1500  # ブラックジャックは3:2
    assert_clean(bot)


def test_blackjack_double_down_doubles_bet_and_payout(bot):
    ctx, view = make_bj(bot, 1000, ("5", "6"), ("10", "7"), deck=("10",))  # 11 + 10 = 21 で勝ち
    pressed = FakeInteraction()
    run(view.double_button.callback(pressed))
    assert ctx.bet == 2000 and view.game.doubled
    assert bot.economy.balance(1) == INITIAL_BALANCE + 2000  # 賭け金2000が倍になって戻る
    assert_clean(bot) and ledger_matches_balance(bot)


def test_blackjack_double_refused_when_even_teiai_credit_cannot_cover_it(bot):
    bot.economy.credit(1, 10_000, "テスト")
    bot.economy.debit_up_to(1, bot.economy.balance(1) - 5_100, "テスト")  # 残高 5,100
    ctx, view = make_bj(
        bot, 5_000, ("5", "6"), ("10", "7"), deck=("10",)
    )  # 預けて残り 100 → 足りない 4,900 は信用の枠（3,000）超え
    pressed = FakeInteraction()
    run(view.double_button.callback(pressed))
    assert not view.game.doubled and pressed.sent and pressed.sent[0]["ephemeral"]
    assert bot.economy.escrow_amount(ctx.escrow_id) == 5_000 and bot.economy.debt(1) == 0
    ctx.abort()
    assert_clean(bot)


def test_blackjack_double_on_teiai_credit_after_confirmation(bot, monkeypatch):
    async def agree(interaction, title, description, label):
        return True

    monkeypatch.setattr(play_bj, "confirm_action", agree)
    bot.economy.debit_up_to(1, INITIAL_BALANCE - 1200, "テスト")  # 残高 1,200
    ctx, view = make_bj(
        bot, 1000, ("5", "6"), ("10", "7"), deck=("10",)
    )  # 預けて残り 200 → 足りない 800 を、帝愛から借りる
    run(view.double_button.callback(FakeInteraction()))
    assert view.game.doubled and ctx.bet == 2000
    assert (
        bot.economy.debt(1) == 200 and view.origin.edits
    )  # 800 借りた。勝って、純利益2,000の3割（600）が天引きで返済され、残り200
    assert_clean(bot)


def test_blackjack_double_on_credit_is_cancelled_without_borrowing(bot, monkeypatch):
    async def decline(interaction, title, description, label):
        return False

    monkeypatch.setattr(play_bj, "confirm_action", decline)
    bot.economy.debit_up_to(1, INITIAL_BALANCE - 1200, "テスト")
    ctx, view = make_bj(bot, 1000, ("5", "6"), ("10", "7"), deck=("10",))
    run(view.double_button.callback(FakeInteraction()))
    assert not view.game.doubled and bot.economy.debt(1) == 0 and bot.economy.escrow_amount(ctx.escrow_id) == 1000
    ctx.abort()
    assert_clean(bot)


def test_blackjack_timeout_stands_and_settles(bot):
    ctx, view = make_bj(bot, 500, ("10", "9"), ("10", "7"))
    run(view.on_timeout())
    assert ctx.done and bot.economy.balance(1) == INITIAL_BALANCE + 500
    assert_clean(bot)


def test_only_the_bettor_can_press_buttons(bot):
    ctx, view = make_bj(bot, 100, ("10", "9"), ("10", "7"))
    other = FakeInteraction(uid=2)
    assert run(view.interaction_check(other)) is False and other.sent[0]["ephemeral"]
    assert run(view.interaction_check(FakeInteraction(uid=1))) is True
    ctx.abort()


def test_view_error_refunds_bet(bot):
    ctx, view = make_bj(bot, 800, ("10", "9"), ("10", "7"))
    it = FakeInteraction()
    it.channel = SimpleNamespace(send=lambda **k: asyncio.sleep(0))
    run(view.on_error(it, RuntimeError("x"), None))
    assert bot.economy.balance(1) == INITIAL_BALANCE and view.ctx.done
    assert_clean(bot)


# ------------------------- コマンド定義の検証 ------------------------- #
def _names(payload):
    yield payload["name"]
    for o in payload.get("options", []):
        yield from _names(o)


def test_all_commands_load_and_follow_discord_rules(bot, tmp_path):
    import glob
    import re

    import discord
    from discord.ext import commands

    cmd_bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    cmd_bot.economy, cmd_bot.sessions, cmd_bot.config = bot.economy, bot.sessions, {}

    async def load():
        for f in sorted(
            g for g in glob.glob("cogs/*.py") if not os.path.basename(g).startswith("_")
        ):  # main.py の読み込みと同じ（_ で始まるのは部品のファイル）
            await cmd_bot.load_extension(f[:-3].replace("/", "."))

    asyncio.run(load())
    payloads = [c.to_dict(cmd_bot.tree) for c in cmd_bot.tree.get_commands()]
    names = [n for p in payloads for n in _names(p)]
    assert {p["name"] for p in payloads} == {
        "ペリカ",
        "カジノ",
        "ショップ",
        "help",
        "証券",
        "運営",
        "クイズ",
        "貸付",
    }
    assert [n for n in names if re.search(r"[A-Z]", n) or not 1 <= len(n) <= 32] == []

    missing = []

    def walk(d, path):
        if d.get("options") and d["options"][0]["type"] in (1, 2):
            for o in d["options"]:
                walk(o, f"{path} {o['name']}")
        else:
            missing.extend(f"{path}/{o['name']}" for o in d.get("options", []) if not o.get("description"))
            assert 1 <= len(d["description"]) <= 100, path

    for p in payloads:
        walk(p, p["name"])
    assert missing == []
    admin = next(p for p in payloads if p["name"] == "運営")  # 管理者用は、DM専用の「運営」にまとまっている
    assert admin["contexts"] == [1, 2] and all(
        o["description"].startswith("[管理者]") for o in admin["options"] if o["type"] != 2
    )
    assert {o["name"] for o in admin["options"]} == {
        "サーバー一覧",
        "サーバー許可",
        "サーバー取消",
        "付与",
        "残高設定",
        "お知らせ",
        "発言削除",
        "抽選",
        "クイズ",
        "イベント",
        "還元率",
        "パチンコ統計",
    }
    quiz = next(p for p in payloads if p["name"] == "クイズ")
    assert {o["name"] for o in quiz["options"]} == {"挑戦", "出題", "取り下げ"}  # サーバー内のクイズに管理者用は出ない
    casino = next(p for p in payloads if p["name"] == "カジノ")
    assert {o["name"] for o in casino["options"]} == {
        "チンチロ",
        "パチンコ",
        "スロット",
        "ブラックジャック",
        "ルーレット",
    }


# ----------------- 抜け道・競合に対する回帰テスト ----------------- #
def test_chinchiro_all_in_is_allowed_and_debt_is_collected_from_next_income(bot):
    it = FakeInteraction()
    run(play_ch.play_chinchiro(it, bot, INITIAL_BALANCE))  # 全財産でも遊べる（倍払いの超過は借金になる）
    assert it.edits and it.edits[-1].get("view") is not None
    it.edits[-1]["view"].ctx.abort()
    ctx, view = make_chinchiro_view(bot, INITIAL_BALANCE, (1, 1, 1), [(2, 2, 3)])
    view.final_embed()
    debt = INITIAL_BALANCE * 4 + teiai.SEED
    assert bot.economy.debt(1) == debt and bot.economy.balance(1) == teiai.SEED
    bot.economy.credit(1, 5000, "テスト")  # 付与などの入金は、天引きされない
    assert bot.economy.balance(1) == teiai.SEED + 5000 and bot.economy.debt(1) == debt
    ok, paid = bot.economy.repay_debt(1)  # 自分から返す
    assert ok and paid == teiai.SEED + 5000 and bot.economy.balance(1) == 0 and bot.economy.debt(1) == debt - paid
    bot.economy.credit(1, 20_000, "テスト")
    ok, paid2 = bot.economy.repay_debt(1)  # 完済して、残りが残高になる
    assert ok and bot.economy.debt(1) == 0 and bot.economy.balance(1) == 20_000 - (debt - paid)
    assert_clean(bot) and ledger_matches_balance(bot)


def test_chinchiro_loss_is_always_fully_collectable_at_max_bet(bot):
    """残高の1/10を賭けて親のピンゾロ×自分のヒフミ（10倍払い）で負けても、払い切れる（上限で頭打ちにならない）。"""
    bet = INITIAL_BALANCE // 10
    ctx, view = make_chinchiro_view(bot, bet, (1, 1, 1), [(3, 2, 1)])
    view.final_embed()
    assert ctx.penalty_taken == bet * 9
    assert bot.economy.balance(1) == INITIAL_BALANCE - bet * 10


def test_chinchiro_timeout_and_button_race_settles_once(bot):
    ctx, view = make_chinchiro_view(bot, 500, (1, 1, 1), [])  # 親のピンゾロ。子は放置
    first = view.final_embed()
    balance = bot.economy.balance(1)
    second = view.final_embed()  # 競合して2回呼ばれても
    assert first is second and bot.economy.balance(1) == balance  # 倍払いが二重に引かれない
    assert balance == INITIAL_BALANCE - 500 * 5


def test_chinchiro_deleting_message_after_seeing_dealer_roll_settles_as_menashi(bot, monkeypatch):
    """画面を消しても、目なしで振ったものとして精算される（強い親を見てから消して損を減らせない）。"""
    from games import chinchiro as rules

    strong = [((1, 1, 1), rules.Hand(rules.PINZORO))]
    monkeypatch.setattr(rules, "play_hand", lambda rng: strong)
    it = FakeInteraction(fail_edit_after=0, fail_with=http_error(404))  # 親の出目を見せる編集で、メッセージが消えていた
    run(play_ch.play_chinchiro(it, bot, 300))
    assert bot.economy.balance(1) == INITIAL_BALANCE - 300 * 5  # 賭け金＋ピンゾロの5倍払い（4倍分）を引かれる
    assert_clean(bot)


def test_chinchiro_deleting_message_while_rolling_also_settles(bot, monkeypatch):
    from games import chinchiro as rules

    strong = [((1, 1, 1), rules.Hand(rules.PINZORO))]
    monkeypatch.setattr(rules, "play_hand", lambda rng: strong)
    monkeypatch.setattr(
        rules, "roll_dice", lambda rng: (1, 2, 4)
    )  # 振る目も固定する（ヒフミが出ると、倍払いが10倍になって結果が変わる）
    ctx, view = make_chinchiro_view(bot, 300, (1, 1, 1), [(1, 2, 4), (1, 2, 4), (1, 2, 4)])
    it = FakeInteraction(fail_edit_after=0, fail_with=http_error(404))
    run(view.roll.callback(it))
    assert bot.economy.balance(1) == INITIAL_BALANCE - 300 * 5  # 返金されない
    assert_clean(bot)


def test_chinchiro_server_error_during_dealer_roll_refunds(bot, monkeypatch):
    monkeypatch.setattr(play_ch, "RNG", random.Random(4))
    it = FakeInteraction(fail_edit_after=0, fail_with=http_error(503))  # Discord 側の障害は、遊ぶ人のせいではない
    with pytest.raises(Exception):
        run(play_ch.play_chinchiro(it, bot, 300))
    assert bot.economy.balance(1) == INITIAL_BALANCE
    assert_clean(bot)


def test_blackjack_double_press_is_ignored_after_finish(bot):
    ctx, view = make_bj(bot, 1000, ("10", "9"), ("10", "7"))
    first, second = FakeInteraction(), FakeInteraction()
    second.response.defer = lambda **k: asyncio.sleep(0)
    run(view.stand_button.callback(first))
    cards = list(view.game.player)
    run(view.hit_button.callback(second))  # 精算後に届いた2回目の操作
    assert view.game.player == cards and bot.economy.balance(1) == INITIAL_BALANCE + 1000


def test_transfer_blocked_while_playing_and_needs_min_games(bot, tmp_path):
    from cogs.economy_cog import TRANSFER_MIN_GAMES, EconomyCog

    eco = bot.economy
    r = eco.transfer(1, 2, 100, 0.1, 20_000, 0.0, TRANSFER_MIN_GAMES)
    assert not r.ok and "回以上" in r.reason  # 1回も遊んでいない人は送れない
    for _ in range(TRANSFER_MIN_GAMES):
        eco.settle(eco.place_bet(1, 10, "テスト"), 10, "テスト")
    assert eco.transfer(1, 2, 100, 0.1, 20_000, 0.0, TRANSFER_MIN_GAMES).ok
    # ゲーム中の送金はコマンド側で止める
    cog = EconomyCog(bot)
    bot.sessions.try_start(1)
    it = FakeInteraction()
    it.guild = SimpleNamespace(id=10)
    member = SimpleNamespace(id=2, bot=False, mention="@b")
    run(EconomyCog.transfer.callback(cog, it, member, 100))
    assert "遊んでいる" in it.sent[0]["embed"].description
    bot.sessions.end(1)


def test_transfer_fee_rounds_up_and_does_not_count_toward_limit(bot):
    eco = bot.economy
    for _ in range(3):
        eco.settle(eco.place_bet(1, 10, "テスト"), 10, "テスト")
    r = eco.transfer(1, 2, 5, 0.1, 20_000, 0.0, 3)
    assert r.ok and r.fee == 1  # 少額でも手数料は切り上げ
    eco.credit(1, 100_000, "テスト")
    assert eco.transfer(1, 2, 1000, 0.1, 1200, 0.0, 3).ok  # 送った額は 5 + 1000 = 1005（手数料は数えない）
    assert not eco.transfer(1, 2, 200, 0.1, 1200, 0.0, 3).ok  # 1205 になるので上限を超える
    assert eco.transfer(1, 2, 195, 0.1, 1200, 0.0, 3).ok  # ちょうど上限の 1200 までは送れる


def test_balance_of_others_does_not_create_accounts(bot):
    assert bot.economy.peek_stats(99) is None
    assert bot.economy._conn.execute("SELECT COUNT(*) FROM users WHERE user_id=99").fetchone()[0] == 0


def test_daily_cannot_be_claimed_again_if_clock_goes_back(bot):
    import datetime

    d = datetime.date(2026, 10, 20)
    assert bot.economy.claim_daily(1, d).ok
    assert not bot.economy.claim_daily(1, d - datetime.timedelta(days=1)).ok  # 時計が戻っても再受取できない


def test_failed_start_does_not_leave_the_user_locked(bot):
    it = FakeInteraction()
    assert run(common.start_game(it, bot, 5, "テスト")) is None  # 範囲外
    assert not bot.sessions.is_active(1)
    assert run(common.start_game(FakeInteraction(), bot, 100, "テスト")) is not None


def test_second_instance_is_refused_by_the_lock(tmp_path, monkeypatch):
    import fcntl
    import importlib
    import sys

    import pytest as _pytest

    # main.py はインポート時に config.json を読むため、ロックの動きだけを同じ方法で確かめる
    path = tmp_path / "bot.lock"
    first = open(path, "w")
    fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
    second = open(path, "w")
    with _pytest.raises(OSError):
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 2つ目は取れない
    first.close()
    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 1つ目が終われば取れる
    second.close()
    assert importlib and sys


def test_win_result_shows_streak_and_mission_celebration(bot, monkeypatch):
    from core import missions

    monkeypatch.setattr(
        missions,
        "today_missions",
        lambda day: [missions.BY_ID["streak2"], missions.BY_ID["play5"], missions.BY_ID["win3"]],
    )

    monkeypatch.setattr(simple.roulette, "spin", lambda rng: 1)  # 出目は必ず赤の1
    texts = []
    for _ in range(2):
        it = FakeInteraction()
        run(simple.play_roulette(it, bot, 1000, "red"))
        e = (it.edits[-1] if it.edits else it.sent[-1])["embed"]
        texts.append(e.description)
    assert "連勝" not in texts[0] and "2連勝中" in texts[1]
    assert "ミッション達成" in texts[1] and "2連勝する" in texts[1]
    assert ledger_matches_balance(bot)


def test_result_title_shows_profit_and_order_puts_dice_before_mission_notice(bot, monkeypatch):
    from core import missions

    monkeypatch.setattr(
        missions,
        "today_missions",
        lambda day: [missions.BY_ID["play5"], missions.BY_ID["win3"], missions.BY_ID["wager5k"]],
    )

    monkeypatch.setattr(simple.roulette, "spin", lambda rng: 1)  # 出目は必ず赤の1
    for _ in range(5):
        it = FakeInteraction()
        run(simple.play_roulette(it, bot, 1000, "red"))
    e = (it.edits[-1] if it.edits else it.sent[-1])["embed"]
    assert "ペリカ" in e.title and "+1,000" in e.title  # タイトルに損益
    d = e.description
    assert d.index("あなたの賭け") < d.index("賭け金") < d.index("ミッション達成")  # 出目 → 数字 → ミッション通知


def test_help_is_short_and_has_a_page_per_category():
    import asyncio

    from cogs.help_cog import BUTTONS, HOME, Help, HelpView, page_embed

    it = type("I", (), {})()
    sent = []

    async def send(**kw):
        sent.append(kw)

    it.response = type("R", (), {"send_message": staticmethod(send)})()
    asyncio.run(Help.help.callback(Help(None), it))
    e = sent[0]["embed"]
    assert (
        "はじめの3ステップ" in e.description and sent[0]["ephemeral"] is True and len(e.description) < 400
    )  # 最初の画面は短く
    view = sent[0]["view"]
    assert [b.label for b in view.children] == [label for _, label in BUTTONS]
    text = ""
    for key, _ in BUTTONS:
        page = page_embed(key)
        text += page.title + page.description
        assert len(page.description) <= 1000 and len(page) <= 6000  # Discord の上限
    assert "7️⃣" in text and "還元率" not in text and "期待値" not in text and "借金" in text and "空売り" in text
    assert "サーバー許可" not in text and "ランキング掲示" in page_embed("admin").description
    assert (
        "ランキング掲示" not in page_embed(HOME).description
        and "ランキング掲示" not in page_embed("pelica").description
    )  # 管理者のコマンドを初心者向けに混ぜない

    edits = []

    async def edit_message(**kw):
        edits.append(kw)

    click = type("I", (), {})()
    click.response = type("R", (), {"edit_message": staticmethod(edit_message)})()
    asyncio.run(view.children[1].callback(click))  # 「ゲーム」ボタン
    assert edits[0]["embed"].title == page_embed("game").title and isinstance(edits[0]["view"], HelpView)


def _last_view(it):
    return next(e["view"] for e in reversed(it.edits) if e.get("view") is not None)


@pytest.mark.parametrize("game", ["slot", "roulette", "pachinko"])
def test_replay_button_plays_again_with_same_and_double_bet(bot, monkeypatch, game):
    from play import pachinko as play_pa

    monkeypatch.setattr(simple, "STEP", 0)
    monkeypatch.setattr(play_pa, "STEP", 0)
    start = {
        "slot": lambda it: simple.play_slot(it, bot, 100),
        "roulette": lambda it: simple.play_roulette(it, bot, 100, "red"),
        "pachinko": lambda it: play_pa.play_pachinko(it, bot, 100),
    }[game]
    it = FakeInteraction()
    run(start(it))
    view = _last_view(it)
    assert "同じ額" in view.again.label and "200" in view.double.label
    again = FakeInteraction()
    run(view.again.callback(again))  # 同じ額でもう一度
    assert any("embed" in k for k in again.sent + again.edits) and again.edits and "ペリカ" in title_of(again)
    assert bot.economy.stats(1)["games_played"] == 2
    dbl = FakeInteraction()
    run(view.double.callback(dbl))
    assert bot.economy.stats(1)["games_played"] == 3 and ledger_matches_balance(bot) and assert_clean(bot) is None


def test_replay_button_only_for_the_player_and_double_is_always_offered(bot, monkeypatch):
    monkeypatch.setattr(simple, "STEP", 0)
    it = FakeInteraction()
    run(simple.play_roulette(it, bot, 100, "red"))
    view = _last_view(it)
    other = FakeInteraction(uid=2)
    assert run(view.interaction_check(other)) is False and "他の人" in other.sent[-1]["content"]
    from play.common import ReplayView

    assert any(
        "倍にして（2,000,000）" in (c.label or "") for c in ReplayView(bot, 1, 1_000_000, None).children
    )  # 上限がないので、大きな額でも出る
    assert any("倍にして" in (c.label or "") for c in view.children)


def test_chinchiro_result_has_replay_button_with_original_bet(bot, monkeypatch):
    monkeypatch.setattr(play_ch, "STEP", 0)
    ctx, view = make_chinchiro_view(bot, 300, (1, 2, 4), [])
    monkeypatch.setattr(ch, "roll_dice", lambda rng: (4, 5, 6))  # シゴロで、1回で決着
    it = FakeInteraction()
    run(view.roll.callback(it))  # 振って精算まで進める
    replay = _last_view(it)
    assert "同じ額" in replay.again.label and "300" in replay.again.label
    again = FakeInteraction()
    run(replay.again.callback(again))
    assert (
        bot.sessions.try_start(1) is not None and "地下チンチロ" in again.sent[-1]["embed"].title
    )  # 新しいゲームが始まって、親が振っている


def test_blackjack_result_replay_uses_the_original_bet_not_the_doubled_one(bot):
    ctx, view = make_bj(bot, 1000, ("5", "6"), ("10", "7"), deck=("10",))
    it = FakeInteraction()
    run(view.double_button.callback(it))  # ダブルで賭け金が2000になる
    replay = _last_view(it)
    assert "1,000" in replay.again.label and "2,000" in replay.double.label
    assert it.edits[-1]["view"] is replay


def test_blackjack_double_win_shows_flair_counts_for_mission_and_has_no_other_game_hint(bot, monkeypatch):
    from core import missions

    monkeypatch.setattr(
        missions,
        "today_missions",
        lambda day: [missions.BY_ID["bj_double"], missions.BY_ID["game_スロット"], missions.BY_ID["play5"]],
    )
    ctx, view = make_bj(bot, 1000, ("5", "6"), ("10", "7"), deck=("10",))  # 11 に10を引いて21。ダブルで勝つ
    it = FakeInteraction()
    run(view.double_button.callback(it))
    text = it.edits[-1]["embed"].description
    assert (
        "ダブルダウン成功" in text and "ミッション達成" in text and "ブラックジャックでダブルダウンに成功する" in text
    )
    assert "別のゲーム" not in text and "👉" not in text


def test_blackjack_natural_flair(bot):
    ctx, view = make_bj(bot, 1000, ("A", "K"), ("10", "7"))
    assert "ナチュラルブラックジャック" in view.final_embed().description


def _all_titles(it):
    return [e["embed"].title for e in it.sent + it.edits if e.get("embed") is not None]


@pytest.mark.parametrize("game", ["slot", "roulette", "pachinko", "chinchiro", "blackjack"])
def test_every_game_title_shows_the_players_name(bot, monkeypatch, game):
    from play import pachinko as play_pa

    monkeypatch.setattr(simple, "STEP", 0)
    monkeypatch.setattr(play_pa, "STEP", 0)
    monkeypatch.setattr(play_ch, "STEP", 0)
    it = FakeInteraction()  # display_name は「テスト」
    if game == "slot":
        run(simple.play_slot(it, bot, 100))
    elif game == "roulette":
        run(simple.play_roulette(it, bot, 100, "red"))
    elif game == "pachinko":
        run(play_pa.play_pachinko(it, bot, 100))
    elif game == "chinchiro":
        run(play_ch.play_chinchiro(it, bot, 100))
    else:
        run(play_bj.play_blackjack(it, bot, 100))
    titles = _all_titles(it)
    assert titles and all("（テスト）" in t for t in titles), titles


# ------------------- 演出の表示に失敗したときの記録 ------------------- #
def test_display_failure_is_logged_with_game_step_and_status_and_settlement_still_holds(bot, capsys):
    it = FakeInteraction(fail_edit_after=1, fail_with=http_error(503))  # 途中のコマの書き換えで、Discord 側の障害
    run(simple.play_roulette(it, bot, 100, "red"))
    out = capsys.readouterr().out
    assert "演出の表示に失敗: ルーレット/途中のコマ 2/2: " in out and "status=503" in out
    assert_clean(bot)  # 精算は済み、次のゲームを始められる
    assert ledger_matches_balance(bot)


@pytest.mark.parametrize(
    "play, name",
    [
        (lambda it, bot: simple.play_slot(it, bot, 100), "スロット"),
        (lambda it, bot: play_pa.play_pachinko(it, bot, 100), "パチンコ"),
    ],
)
def test_display_failure_is_logged_for_every_animated_game(bot, capsys, play, name):
    it = FakeInteraction(fail_edit_after=0, fail_with=http_error(404))  # 最初の書き換えで、メッセージが消えていた
    run(play(it, bot))
    out = capsys.readouterr().out
    assert f"演出の表示に失敗: {name}/" in out and "status=404" in out
    assert_clean(bot)


def test_log_display_failure_handles_errors_without_status(capsys):
    common.log_display_failure("テスト", "結果の表示", RuntimeError("boom"))
    assert "演出の表示に失敗: テスト/結果の表示: RuntimeError status=? code=?: boom" in capsys.readouterr().out


# ------------------- パチンコ: 連続投入とボタン ------------------- #
def test_pachinko_sets_play_in_one_go_and_settle_once(bot, monkeypatch):
    monkeypatch.setattr(play_pa, "RNG", random.Random(7))
    monkeypatch.setattr(play_pa, "STEP", 0)
    it = FakeInteraction()
    run(play_pa.play_pachinko(it, bot, 200, 5))  # 200 × 5セット = 1,000
    stats = bot.economy.pachinko_stats()
    assert stats["sets"][0] == 5 and stats["sets"][2] == 5 * 200 and bot.economy.stats(1)["total_wagered"] == 1000
    assert bot.economy.stats(1)["games_played"] == 1  # 連続投入は、1回の遊びとして数える
    assert "×" in it.sent[0]["embed"].description and "5セット" in it.sent[0]["embed"].description
    final = it.edits[-1]["embed"]
    assert "5セット" in final.description or "セット" in final.description
    assert len(it.edits) == 2 + 5 + 1 and ledger_matches_balance(bot)  # 打ち出し2コマ + セットの数ぶん + 結果
    assert_clean(bot)


def test_pachinko_sets_total_has_no_cap_but_needs_the_balance_and_nothing_is_charged_otherwise(bot):
    it = FakeInteraction()
    run(play_pa.play_pachinko(it, bot, 1_000_000, 2))  # 合計200万は、残高に足りない
    assert (
        "足りません" in it.sent[0]["embed"].title
        and bot.economy.balance(1) == INITIAL_BALANCE
        and bot.economy.pachinko_stats()["sets"][0] == 0
    )


def test_pachinko_with_a_skin_changes_only_the_look(bot, monkeypatch):
    from core import shop, skins

    monkeypatch.setattr(play_pa, "STEP", 0)
    eco = bot.economy
    eco.credit(1, 2_000_000, "テスト")
    vip = shop.BY_ID["skin_vip"]
    assert eco.purchase(1, vip.id, shop.label(vip), vip.price, vip.unique).ok
    results = {}
    for equipped in (False, True):
        eco.equip(1, "skin_table", vip.id if equipped else None)
        monkeypatch.setattr(play_pa, "RNG", random.Random(3))  # 同じ乱数なら、同じ結果になるはず
        it = FakeInteraction()
        run(play_pa.play_pachinko(it, bot, 100, 1))
        results[equipped] = it
    plain, skinned = results[False], results[True]
    title = lambda it: it.edits[-1]["embed"].title  # noqa: E731
    assert not title(plain).startswith("🥀") and title(skinned).startswith("🥀")  # タイトルに飾りが付く
    assert title(skinned).split(" — ")[1] == title(plain).split(" — ")[1]  # 損益は同じ（配当は変わらない）
    body = skinned.edits[-1]["embed"].description
    assert skins.SKINS["skin_vip"].win_line in body or skins.SKINS["skin_vip"].lose_line in body
    assert skins.SKINS["skin_vip"].start_line in skinned.sent[0]["embed"].description  # 始まりの地の文


def test_pachinko_buttons_again_double_and_extra_sets(bot, monkeypatch):
    monkeypatch.setattr(play_pa, "RNG", random.Random(3))
    monkeypatch.setattr(play_pa, "STEP", 0)
    bot.economy.credit(1, 50_000, "テスト")
    it = FakeInteraction()
    run(play_pa.play_pachinko(it, bot, 100, 1))
    view = _last_view(it)
    labels = [c.label for c in view.children]
    assert any("同じ額" in label for label in labels) and any(
        "×5連続（500）" in label for label in labels
    )  # 1セットのときは ×5連続
    extra = next(c for c in view.children if "×5連続" in c.label)
    again = FakeInteraction()
    run(extra.callback(again))
    assert bot.economy.pachinko_stats()["sets"][0] == 1 + 5
    view5 = _last_view(again)
    assert any("×10連続（1,000）" in c.label for c in view5.children)  # 5セットのあとは ×10連続
    same = FakeInteraction()
    run(next(c for c in view5.children if "同じ額" in c.label).callback(same))
    assert bot.economy.pachinko_stats()["sets"][0] == 1 + 5 + 5  # 同じ額 = 同じセット数のまま
    big = FakeInteraction()
    run(play_pa.play_pachinko(big, bot, 100, 10))
    assert not any(
        "連続" in c.label for c in _last_view(big).children if c.label
    )  # 10セットなら、これ以上の連続ボタンはない
    assert ledger_matches_balance(bot)
