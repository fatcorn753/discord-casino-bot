"""Discord 側のゲームの流れ（賭け金の預かり・精算・中断時の返金）を、偽の Interaction で確かめる。"""

import asyncio
import random
from types import SimpleNamespace

import pytest

from core.economy import INITIAL_BALANCE, Economy
from core.session import Sessions
from games import blackjack as bj
from games import chinchiro as ch
from play import blackjack as play_bj
from play import chinchiro as play_ch
from play import common, pachinko as play_pa, simple


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
def test_bet_out_of_range_and_insufficient_are_rejected_without_charging(bot):
    for bet in (1, common.MAX_BET + 1):
        it = FakeInteraction()
        assert run(common.start_game(it, bot, bet, "テスト")) is None
        assert it.sent[0]["ephemeral"] and "範囲外" in it.sent[0]["embed"].title
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
    assert bot.economy.balance(1) == INITIAL_BALANCE - 100      # 2つ目の賭け金は引かれていない
    ctx.settle(0)


# ------------------------- 結果が出るゲーム ------------------------- #
@pytest.mark.parametrize("play", ["slot", "coin", "roulette"])
def test_instant_games_settle_and_keep_ledger_consistent(bot, monkeypatch, play):
    monkeypatch.setattr(simple, "RNG", random.Random(5))
    for i in range(30):
        it = FakeInteraction()
        if play == "slot":
            run(simple.play_slot(it, bot, 100))
        elif play == "coin":
            run(simple.play_coin(it, bot, 100, "表"))
        else:
            run(simple.play_roulette(it, bot, 100, "red"))
        assert_clean(bot)
        assert bot.economy.balance(1) >= 0
    assert ledger_matches_balance(bot)


def test_coin_win_pays_1_95x(bot, monkeypatch):
    class Heads:
        def choice(self, seq):
            return "表"
    monkeypatch.setattr(simple, "RNG", Heads())
    it = FakeInteraction()
    run(simple.play_coin(it, bot, 1000, "表"))
    assert bot.economy.balance(1) == INITIAL_BALANCE - 1000 + 1950


def test_roulette_validates_number_before_charging(bot):
    it = FakeInteraction()
    run(simple.play_roulette(it, bot, 100, "number", None))
    assert "数字が必要" in it.sent[0]["embed"].title and bot.economy.balance(1) == INITIAL_BALANCE
    it = FakeInteraction()
    run(simple.play_roulette(it, bot, 100, "red", 7))
    assert "数字は不要" in it.sent[0]["embed"].title and bot.economy.balance(1) == INITIAL_BALANCE


def http_error(status):
    import discord
    cls = discord.NotFound if status == 404 else discord.HTTPException      # 実際の Discord も 404 は NotFound になる
    return cls(SimpleNamespace(status=status, reason="x"), "err")


def test_display_failure_after_result_does_not_change_the_result(bot, monkeypatch):
    """演出の途中でメッセージが消えても、結果は先に精算済み（外れが返金に変わらない）。"""
    class Tails:
        def choice(self, seq):
            return "裏"
    monkeypatch.setattr(simple, "RNG", Tails())
    it = FakeInteraction(fail_edit_after=1, fail_with=http_error(404))      # 予想は「表」だが結果は「裏」＝ハズレ
    run(simple.play_coin(it, bot, 500, "表"))
    assert bot.economy.balance(1) == INITIAL_BALANCE - 500
    assert_clean(bot)


def test_non_display_error_after_settle_keeps_result(bot, monkeypatch):
    monkeypatch.setattr(simple, "RNG", random.Random(1))
    it = FakeInteraction(fail_edit_after=1)          # 想定外の例外（バグ）
    before = bot.economy.balance(1)
    with pytest.raises(RuntimeError):
        run(simple.play_slot(it, bot, 500))
    after = bot.economy.balance(1)
    reels = simple.slot.spin(random.Random(1))
    assert after == before - 500 + int(500 * simple.slot.multiplier(reels))   # 返金ではなく、精算した結果のまま
    assert_clean(bot)


def test_error_before_settle_refunds_bet(bot, monkeypatch):
    def boom(rng):
        raise ValueError("バグ")
    monkeypatch.setattr(simple.slot, "spin", boom)
    with pytest.raises(ValueError):
        run(simple.play_slot(FakeInteraction(), bot, 500))
    assert bot.economy.balance(1) == INITIAL_BALANCE          # 精算の前に失敗したら、賭け金は返る
    assert bot.economy.stats(1)["games_played"] == 0          # 返金は成績に数えない
    assert_clean(bot)


def test_pachinko_plays_all_frames_and_settles(bot, monkeypatch):
    monkeypatch.setattr(play_pa, "RNG", random.Random(2))
    for _ in range(40):
        it = FakeInteraction()
        run(play_pa.play_pachinko(it, bot, 100))
        assert len(it.edits) >= 9 and "パチンコ" in title_of(it)
        assert_clean(bot)
    assert ledger_matches_balance(bot)


# ------------------------------ チンチロ ------------------------------ #
def make_chinchiro_view(bot, bet, dealer_dice, player_dice_list):
    ctx = run(common.start_game(FakeInteraction(), bot, bet, "チンチロ"))
    dealer_rolls = [(dealer_dice, ch.classify(dealer_dice))]
    view = play_ch.ChinchiroView(ctx, FakeInteraction(), dealer_rolls)
    view.player_rolls = [(d, ch.classify(d)) for d in player_dice_list]
    return ctx, view


def test_chinchiro_win_pays_by_role(bot):
    ctx, view = make_chinchiro_view(bot, 1000, (1, 1, 3), [(1, 1, 1)])          # 子がピンゾロ（5倍）
    view.final_embed()
    assert bot.economy.balance(1) == INITIAL_BALANCE + 5000
    assert_clean(bot)


def test_chinchiro_lose_with_multiplier_is_capped_at_balance(bot):
    # 全財産を賭けて、親のピンゾロ（5倍払い）で負ける: 追加で引かれるのは残高の範囲まで
    ctx, view = make_chinchiro_view(bot, INITIAL_BALANCE, (1, 1, 1), [(2, 2, 3)])
    embed = view.final_embed()
    assert bot.economy.balance(1) == 0
    assert "全財産" in embed.description
    assert_clean(bot) and ledger_matches_balance(bot)


def test_chinchiro_lose_double_extra_comes_from_remaining_balance(bot):
    bot.economy.credit(1, 7000, "テスト")                                   # 残高 10,000
    ctx, view = make_chinchiro_view(bot, 1000, (1, 1, 1), [(2, 2, 3)])       # 親ピンゾロ: 5倍 = 賭け金1000 + 追加4000
    view.final_embed()
    assert bot.economy.balance(1) == 10_000 - 1000 - 4000
    assert ledger_matches_balance(bot)


def test_chinchiro_draw_returns_bet(bot):
    ctx, view = make_chinchiro_view(bot, 500, (2, 2, 4), [(4, 2, 2)])
    view.final_embed()
    assert bot.economy.balance(1) == INITIAL_BALANCE


def test_chinchiro_timeout_settles_as_menashi_loss(bot):
    ctx, view = make_chinchiro_view(bot, 400, (2, 2, 4), [])               # 放置
    run(view.on_timeout())
    assert bot.economy.balance(1) == INITIAL_BALANCE - 400                 # 目なし扱いで負け。賭け金は預かったままにならない
    assert_clean(bot)


def test_chinchiro_full_flow_with_button(bot, monkeypatch):
    monkeypatch.setattr(play_ch, "RNG", random.Random(4))
    it = FakeInteraction()
    run(play_ch.play_chinchiro(it, bot, 300))
    view = it.edits[-1]["view"]
    assert view is not None and not bot.economy._conn.execute("SELECT COUNT(*) FROM escrow").fetchone()[0] == 0
    for _ in range(3):                                          # 押し続ければ必ず終わる（最大3回）
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
    c = lambda *ranks: [(r, "♠") for r in ranks]           # noqa: E731
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
    assert bot.economy.balance(1) == INITIAL_BALANCE + 1000 + 1500        # ブラックジャックは3:2
    assert_clean(bot)


def test_blackjack_double_down_doubles_bet_and_payout(bot):
    ctx, view = make_bj(bot, 1000, ("5", "6"), ("10", "7"), deck=("10",))     # 11 + 10 = 21 で勝ち
    pressed = FakeInteraction()
    run(view.double_button.callback(pressed))
    assert ctx.bet == 2000 and view.game.doubled
    assert bot.economy.balance(1) == INITIAL_BALANCE + 2000                    # 賭け金2000が倍になって戻る
    assert_clean(bot) and ledger_matches_balance(bot)


def test_blackjack_double_refused_when_balance_short(bot):
    bot.economy.debit_up_to(1, INITIAL_BALANCE - 1200, "テスト")             # 残高 1,200
    ctx, view = make_bj(bot, 1000, ("5", "6"), ("10", "7"), deck=("10",))     # 預けて残り 200 → ダブル不可
    pressed = FakeInteraction()
    run(view.double_button.callback(pressed))
    assert not view.game.doubled and pressed.sent and pressed.sent[0]["ephemeral"]
    assert bot.economy.escrow_amount(ctx.escrow_id) == 1000
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
        for f in sorted(g for g in glob.glob("cogs/*.py") if not g.endswith("__init__.py")):
            await cmd_bot.load_extension(f[:-3].replace("/", "."))

    asyncio.run(load())
    payloads = [c.to_dict(cmd_bot.tree) for c in cmd_bot.tree.get_commands()]
    names = [n for p in payloads for n in _names(p)]
    assert {p["name"] for p in payloads} == {"ペリカ", "カジノ", "ショップ", "ヘルプ", "暗号"}
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
    casino = next(p for p in payloads if p["name"] == "カジノ")
    assert {o["name"] for o in casino["options"]} == {"チンチロ", "パチンコ", "スロット", "コイン", "ブラックジャック", "ルーレット"}


# ----------------- 抜け道・競合に対する回帰テスト ----------------- #
def test_chinchiro_all_in_is_rejected_because_of_multiplier_payments(bot):
    it = FakeInteraction()
    run(play_ch.play_chinchiro(it, bot, INITIAL_BALANCE))          # 全財産では倍払いに備えられない
    assert "足りません" in it.sent[0]["embed"].title and "5倍" in it.sent[0]["embed"].description
    assert bot.economy.balance(1) == INITIAL_BALANCE
    assert_clean(bot)
    it = FakeInteraction()
    run(play_ch.play_chinchiro(it, bot, INITIAL_BALANCE // 5))      # 残高の1/5までなら遊べる
    assert it.edits and it.edits[-1].get("view") is not None
    it.edits[-1]["view"].ctx.abort()


def test_chinchiro_loss_is_always_fully_collectable_at_max_bet(bot):
    """残高の1/5を賭けて親のピンゾロ（5倍払い）で負けても、払い切れる（上限で頭打ちにならない）。"""
    bet = INITIAL_BALANCE // 5
    ctx, view = make_chinchiro_view(bot, bet, (1, 1, 1), [(2, 2, 3)])
    view.final_embed()
    assert ctx.penalty_taken == bet * 4
    assert bot.economy.balance(1) == INITIAL_BALANCE - bet * 5


def test_chinchiro_timeout_and_button_race_settles_once(bot):
    ctx, view = make_chinchiro_view(bot, 500, (1, 1, 1), [])         # 親のピンゾロ。子は放置
    first = view.final_embed()
    balance = bot.economy.balance(1)
    second = view.final_embed()                                       # 競合して2回呼ばれても
    assert first is second and bot.economy.balance(1) == balance      # 倍払いが二重に引かれない
    assert balance == INITIAL_BALANCE - 500 * 5


def test_chinchiro_deleting_message_after_seeing_dealer_roll_settles_as_menashi(bot, monkeypatch):
    """画面を消しても、目なしで振ったものとして精算される（強い親を見てから消して損を減らせない）。"""
    from games import chinchiro as rules
    strong = [((1, 1, 1), rules.Hand(rules.PINZORO))]
    monkeypatch.setattr(rules, "play_hand", lambda rng: strong)
    it = FakeInteraction(fail_edit_after=0, fail_with=http_error(404))      # 親の出目を見せる編集で、メッセージが消えていた
    run(play_ch.play_chinchiro(it, bot, 300))
    assert bot.economy.balance(1) == INITIAL_BALANCE - 300 * 5              # 賭け金＋ピンゾロの5倍払い（4倍分）を引かれる
    assert_clean(bot)


def test_chinchiro_deleting_message_while_rolling_also_settles(bot, monkeypatch):
    from games import chinchiro as rules
    strong = [((1, 1, 1), rules.Hand(rules.PINZORO))]
    monkeypatch.setattr(rules, "play_hand", lambda rng: strong)
    ctx, view = make_chinchiro_view(bot, 300, (1, 1, 1), [(1, 2, 4), (1, 2, 4), (1, 2, 4)])
    it = FakeInteraction(fail_edit_after=0, fail_with=http_error(404))
    run(view.roll.callback(it))
    assert bot.economy.balance(1) == INITIAL_BALANCE - 300 * 5              # 返金されない
    assert_clean(bot)


def test_chinchiro_server_error_during_dealer_roll_refunds(bot, monkeypatch):
    monkeypatch.setattr(play_ch, "RNG", random.Random(4))
    it = FakeInteraction(fail_edit_after=0, fail_with=http_error(503))      # Discord 側の障害は、遊ぶ人のせいではない
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
    run(view.hit_button.callback(second))                  # 精算後に届いた2回目の操作
    assert view.game.player == cards and bot.economy.balance(1) == INITIAL_BALANCE + 1000


def test_transfer_blocked_while_playing_and_needs_min_games(bot, tmp_path):
    from cogs.economy_cog import EconomyCog, TRANSFER_MIN_GAMES
    eco = bot.economy
    r = eco.transfer(1, 2, 100, 0.1, 20_000, 0.0, TRANSFER_MIN_GAMES)
    assert not r.ok and "回以上" in r.reason                                  # 1回も遊んでいない人は送れない
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


def test_transfer_fee_rounds_up_and_counts_toward_limit(bot):
    eco = bot.economy
    for _ in range(3):
        eco.settle(eco.place_bet(1, 10, "テスト"), 10, "テスト")
    r = eco.transfer(1, 2, 5, 0.1, 20_000, 0.0, 3)
    assert r.ok and r.fee == 1                              # 少額でも手数料は切り上げ
    eco.credit(1, 100_000, "テスト")
    assert eco.transfer(1, 2, 1000, 0.1, 1200, 0.0, 3).ok                       # 1000 + 手数料100 = 1100（+過去の6）
    assert not eco.transfer(1, 2, 100, 0.1, 1200, 0.0, 3).ok                     # 手数料込みの合計で上限を超える


def test_balance_of_others_does_not_create_accounts(bot):
    assert bot.economy.peek_stats(99) is None
    assert bot.economy._conn.execute("SELECT COUNT(*) FROM users WHERE user_id=99").fetchone()[0] == 0


def test_daily_cannot_be_claimed_again_if_clock_goes_back(bot):
    import datetime
    d = datetime.date(2026, 10, 20)
    assert bot.economy.claim_daily(1, d).ok
    assert not bot.economy.claim_daily(1, d - datetime.timedelta(days=1)).ok     # 時計が戻っても再受取できない


def test_failed_start_does_not_leave_the_user_locked(bot):
    it = FakeInteraction()
    assert run(common.start_game(it, bot, 5, "テスト")) is None                   # 範囲外
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
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)        # 2つ目は取れない
    first.close()
    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)            # 1つ目が終われば取れる
    second.close()
    assert importlib and sys


# ----------------------------- /ペリカ 付与 ----------------------------- #
def test_grant_is_for_owners_only(bot):
    from cogs.economy_cog import EconomyCog
    bot.config = {"owner_ids": [1]}
    cog = EconomyCog(bot)
    target = SimpleNamespace(id=2, mention="@b")
    it = FakeInteraction(uid=1)
    it.guild = SimpleNamespace(id=10)
    run(EconomyCog.grant.callback(cog, it, target, 5000))
    assert "付与しました" in it.sent[-1]["embed"].title and bot.economy.balance(2) == INITIAL_BALANCE + 5000
    stranger = FakeInteraction(uid=3)
    stranger.guild = SimpleNamespace(id=10)
    run(EconomyCog.grant.callback(cog, stranger, target, 5000))
    assert "権限" in stranger.sent[-1]["embed"].title and bot.economy.balance(2) == INITIAL_BALANCE + 5000     # 他人は付与できない
    bot.config = {}                                                                                              # 未設定なら誰もできない
    nobody = FakeInteraction(uid=1)
    nobody.guild = SimpleNamespace(id=10)
    run(EconomyCog.grant.callback(cog, nobody, target, 5000))
    assert "権限" in nobody.sent[-1]["embed"].title


def test_win_result_shows_streak_and_mission_celebration(bot, monkeypatch):
    from core import missions
    monkeypatch.setattr(missions, "today_missions", lambda day: [missions.BY_ID["streak2"], missions.BY_ID["play5"], missions.BY_ID["win3"]])

    class Heads:
        def choice(self, seq):
            return "表"
    monkeypatch.setattr(simple, "RNG", Heads())
    texts = []
    for _ in range(2):
        it = FakeInteraction()
        run(simple.play_coin(it, bot, 1000, "表"))
        e = (it.edits[-1] if it.edits else it.sent[-1])["embed"]
        texts.append(e.description)
    assert "連勝" not in texts[0] and "2連勝中" in texts[1]
    assert "ミッション達成" in texts[1] and "2連勝する" in texts[1]
    assert ledger_matches_balance(bot)


def test_result_title_shows_profit_and_order_puts_dice_before_mission_notice(bot, monkeypatch):
    from core import missions
    monkeypatch.setattr(missions, "today_missions", lambda day: [missions.BY_ID["play5"], missions.BY_ID["win3"], missions.BY_ID["wager5k"]])

    class Heads:
        def choice(self, seq):
            return "表"
    monkeypatch.setattr(simple, "RNG", Heads())
    for _ in range(5):
        it = FakeInteraction()
        run(simple.play_coin(it, bot, 1000, "表"))
    e = (it.edits[-1] if it.edits else it.sent[-1])["embed"]
    assert "ペリカ" in e.title and "+950" in e.title                       # タイトルに損益
    d = e.description
    assert d.index("あなたの予想") < d.index("賭け金") < d.index("ミッション達成")      # 出目 → 数字 → ミッション通知


def test_chinchiro_balance_error_shows_the_max_bet(bot):
    it = FakeInteraction()
    run(play_ch.play_chinchiro(it, bot, 1000))                               # 残高3000は賭け金1000の5倍に足りない
    text = it.sent[-1]["embed"].description
    assert "まで賭けられます" in text and "600" in text


def test_help_has_steps_payouts_and_admin_section():
    import asyncio
    from cogs.help_cog import Help
    it = type("I", (), {})()
    sent = []

    async def send(**kw):
        sent.append(kw)
    it.response = type("R", (), {"send_message": staticmethod(send)})()
    asyncio.run(Help.help.callback(Help(None), it))
    e = sent[0]["embed"]
    names = [f.name for f in e.fields]
    assert "はじめの3ステップ" in e.description and any("管理者向け" in n for n in names)
    text = e.description + "\n".join(f.value for f in e.fields)
    assert "7️⃣500倍" in text and "還元率" not in text and "期待値" not in text
    start = next(f.value for f in e.fields if "まずはここから" in f.name)
    assert "サーバー許可" not in start and "ランキング掲示" not in start      # 初心者向けの欄に管理者コマンドを混ぜない
    assert all(len(f.value) <= 1024 for f in e.fields) and len(e) <= 6000      # Discord の埋め込みの上限


def _last_view(it):
    return next(e["view"] for e in reversed(it.edits) if e.get("view") is not None)


@pytest.mark.parametrize("game", ["coin", "slot", "roulette", "pachinko"])
def test_replay_button_plays_again_with_same_and_double_bet(bot, monkeypatch, game):
    from play import pachinko as play_pa
    monkeypatch.setattr(simple, "STEP", 0)
    monkeypatch.setattr(play_pa, "STEP", 0)
    start = {"coin": lambda it: simple.play_coin(it, bot, 100, "表"), "slot": lambda it: simple.play_slot(it, bot, 100),
             "roulette": lambda it: simple.play_roulette(it, bot, 100, "red"), "pachinko": lambda it: play_pa.play_pachinko(it, bot, 100)}[game]
    it = FakeInteraction()
    run(start(it))
    view = _last_view(it)
    assert "同じ額" in view.again.label and "200" in view.double.label
    again = FakeInteraction()
    run(view.again.callback(again))                                             # 同じ額でもう一度
    assert any("embed" in k for k in again.sent + again.edits) and again.edits and "ペリカ" in title_of(again)
    assert bot.economy.stats(1)["games_played"] == 2
    dbl = FakeInteraction()
    run(view.double.callback(dbl))
    assert bot.economy.stats(1)["games_played"] == 3 and ledger_matches_balance(bot) and assert_clean(bot) is None


def test_replay_button_only_for_the_player_and_double_hidden_at_max_bet(bot, monkeypatch):
    monkeypatch.setattr(simple, "STEP", 0)
    it = FakeInteraction()
    run(simple.play_coin(it, bot, 100, "表"))
    view = _last_view(it)
    other = FakeInteraction(uid=2)
    assert run(view.interaction_check(other)) is False and "他の人" in other.sent[-1]["content"]
    from play.common import MAX_BET, ReplayView
    assert not any("倍にして" in (c.label or "") for c in ReplayView(bot, 1, MAX_BET, None).children)
    assert any("倍にして" in (c.label or "") for c in view.children)
