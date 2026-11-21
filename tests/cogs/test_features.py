"""機能のオン・オフ（/運営 機能）: コマンドとボタンの入口で止め、途中のものは止めない。"""

import asyncio
from types import SimpleNamespace

import pytest

from cogs.casino import common
from cogs.shared import approval_guard
from domain import features
from domain.types import INITIAL_BALANCE
from tests.fakes import as_interaction, must
from tests.support import make_casino_deps, make_test_bot

G = 10


@pytest.fixture
def bot(tmp_path):
    b = make_test_bot(str(tmp_path / "t.db"))
    yield b
    b.repos.close()


class Response:
    def __init__(self):
        self.sent, self.done = [], False

    def is_done(self):
        return self.done

    async def send_message(self, *args, **kw):
        self.sent.append(kw)
        self.done = True


def interaction(bot, command=None, uid=1):
    return SimpleNamespace(
        guild_id=G,
        user=SimpleNamespace(id=uid, display_name="テスト"),
        client=bot,
        command=SimpleNamespace(qualified_name=command) if command else None,
        response=Response(),
        followup=SimpleNamespace(),
    )


def test_commands_map_to_features():
    of = features.feature_of_command
    assert of("カジノ スロット") == "slot" and of("カジノ チンチロ") == "chinchiro"
    assert of("闘技場 大会") == "arena" and of("競馬 出馬表") == "horse"
    assert of("証券 注文 現物") == "market" and of("証券 注文 債券") == "bonds"
    assert of("証券 保有") is None and of("証券 決済") is None  # 手じまいは、止めていてもできる
    assert of("貸付 返済") is None and of("貸付 提案") == "loan"
    assert of("共済 状況") is None and of("共済 入札") == "kyosai"
    assert of("ペリカ 残高") is None and of("運営 機能 停止") is None
    assert set(features.COMMANDS.values()) <= set(features.FEATURES)
    assert set(features.CASINO_GAMES.values()) <= set(features.FEATURES)


def test_switch_is_per_server_and_idempotent(bot):
    repo = bot.repos.guild
    repo.set_feature(G, "arena", False)
    repo.set_feature(G, "arena", False)
    assert repo.features_off(G) == {"arena"} and repo.features_off(G + 1) == set()
    repo.set_feature(G, "arena", True)
    repo.set_feature(G, "arena", True)
    assert repo.features_off(G) == set()


def test_command_of_a_stopped_feature_is_refused(bot):
    bot.repos.guild.set_feature(G, "arena", False)
    it = interaction(bot, "闘技場 大会")
    assert asyncio.run(approval_guard.command_feature_guard(as_interaction(it))) is False
    assert "停止中" in must(it.response.sent[0]["embed"].title) and it.response.sent[0]["ephemeral"]
    other = interaction(bot, "カジノ スロット")
    assert asyncio.run(approval_guard.command_feature_guard(as_interaction(other))) is True and not other.response.sent


def test_a_stopped_casino_game_cannot_start_even_from_the_replay_button(bot):
    deps = make_casino_deps(bot)
    bot.repos.guild.set_feature(G, "slot", False)
    it = interaction(bot)
    assert asyncio.run(common.start_game(as_interaction(it), deps, 100, "スロット")) is None
    assert "停止中" in must(it.response.sent[0]["embed"].title)
    assert bot.repos.for_guild(G).wallet.balance(1) == INITIAL_BALANCE  # 賭け金は預からない
    ctx = asyncio.run(common.start_game(as_interaction(interaction(bot)), deps, 100, "ルーレット"))
    assert ctx is not None  # ほかのゲームは遊べる
    ctx.settle(0)
