"""ペリカの記録がサーバーごとに別々であること、古い記録の引き継ぎ。"""

import asyncio
import datetime
import os
import random
import sqlite3
from types import SimpleNamespace
from typing import cast

import pytest

from domain import shop
from domain.types import DAILY_BASE, INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import as_interaction, invoke
from tests.support import make_casino_deps, make_economy_cog, make_shop_cog, make_test_bot

pytestmark = pytest.mark.real_scope  # この中は、本物の for_guild（サーバーごとに分かれる）で確かめる

A, B = 10, 20
U1, U2 = 1, 2


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"), strict=True)


# --------------------------- 分離 --------------------------- #
def test_each_server_has_its_own_balance_and_initial_grant(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    assert a.wallet.balance(U1) == INITIAL_BALANCE and b.wallet.balance(U1) == INITIAL_BALANCE  # サーバーごとに初期支給
    a.wallet.credit(U1, 5_000, "テスト")
    assert a.wallet.balance(U1) == INITIAL_BALANCE + 5_000 and b.wallet.balance(U1) == INITIAL_BALANCE


def test_daily_streak_and_claim_are_per_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    day = datetime.date(2026, 10, 20)
    assert a.wallet.claim_daily(U1, day).ok
    assert not a.wallet.claim_daily(U1, day).ok  # Aではもう受け取り済み
    assert b.wallet.claim_daily(U1, day).ok  # Bではまだ受け取れる
    assert (
        a.wallet.claim_daily(U1, day + datetime.timedelta(days=1)).streak == 2
        and b.wallet.stats(U1)["daily_streak"] == 1
    )


def test_ranking_only_counts_this_servers_balances(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    a.wallet.credit(U1, 1_000, "テスト")
    b.wallet.credit(U1, 90_000, "テスト")  # Bで大金持ちでも、Aのランキングには影響しない
    b.wallet.credit(U2, 10, "テスト")
    assert [(r, u, bal) for r, u, bal in a.wallet.top(10)] == [(1, U1, INITIAL_BALANCE + 1_000)]
    assert [u for _, u, _ in b.wallet.top(10)] == [U1, U2]
    assert a.wallet.rank_of(U2) is None and b.wallet.rank_of(U2)[0] == 2


def test_history_and_stats_are_per_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    a.wallet.credit(U1, 500, "Aだけ")
    assert any(h["reason"] == "Aだけ" for h in a.wallet.history(U1, 10))
    assert not any(h["reason"] == "Aだけ" for h in b.wallet.history(U1, 10))
    eid = a.escrow.place_bet(U1, 100, "テスト")
    a.escrow.settle(eid, 300, "テスト")
    assert a.wallet.stats(U1)["games_played"] == 1 and b.wallet.stats(U1)["games_played"] == 0


def test_shop_items_and_equipment_are_per_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    it = shop.BY_ID["title_rookie"]
    assert a.shop.purchase(U1, it.id, shop.label(it), it.price, True).ok
    a.shop.equip(U1, "title", it.id)
    assert a.shop.inventory(U1) == {it.id: 1} and a.shop.get_profile(U1)["title"] == it.id
    assert b.shop.inventory(U1) == {} and b.shop.get_profile(U1)["title"] is None  # Bには持ち込めない
    assert not b.shop.equip(U1, "title", it.id)  # Bでは装備もできない
    assert b.wallet.balance(U1) == INITIAL_BALANCE  # Aでの買い物はBの残高に響かない


def test_consumables_are_per_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    for e in (a, b):
        e.wallet.credit(U1, 10_000, "テスト")
        e.shop.purchase(U1, shop.INSURANCE, "保険券", 2_000, False)
    assert a.shop.arm(U1, shop.INSURANCE)
    eid_b = b.escrow.place_bet(U1, 1_000, "テスト")
    assert b.escrow.settle_with_penalty(eid_b, 0, "テスト", 0)[2] == 0  # Bでは保険は効かない（Aで使った券）
    eid_a = a.escrow.place_bet(U1, 1_000, "テスト")
    assert a.escrow.settle_with_penalty(eid_a, 0, "テスト", 0)[2] == 500


def test_transfer_stays_inside_one_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    for _ in range(3):
        a.escrow.settle(a.escrow.place_bet(U1, 10, "テスト"), 10, "テスト")
    res = a.wallet.transfer(U1, U2, 500, 0.1, 20_000, 0.0, 3)
    assert res.ok
    assert (
        a.wallet.balance(U2) == INITIAL_BALANCE + 500 and b.wallet.balance(U2) == INITIAL_BALANCE
    )  # 別サーバーの口座は動かない
    assert not b.wallet.transfer(U1, U2, 100, 0.1, 20_000, 0.0, 3).ok  # Bでは、まだ3回遊んでいない


def test_escrow_cannot_be_settled_from_another_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    eid = a.escrow.place_bet(U1, 1_000, "テスト")
    assert b.escrow.escrow_amount(eid) == 0
    assert (
        b.escrow.settle(eid, 999_999, "不正") is None and b.escrow.refund(eid) is False
    )  # 他サーバーの預かり金は触れない
    assert a.escrow.escrow_amount(eid) == 1_000
    assert a.escrow.settle(eid, 0, "テスト") == -1_000


def test_refund_on_startup_goes_back_to_the_right_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    a.escrow.place_bet(U1, 700, "A")
    b.escrow.place_bet(U1, 400, "B")
    assert eco.escrow.refund_all_escrows() == 2
    assert a.wallet.balance(U1) == INITIAL_BALANCE and b.wallet.balance(U1) == INITIAL_BALANCE


def test_strict_economy_refuses_unscoped_use(eco):
    for call in (
        lambda: eco.wallet.balance(U1),
        lambda: eco.wallet.top(10),
        lambda: eco.wallet.claim_daily(U1, datetime.date.today()),
    ):
        with pytest.raises(RuntimeError, match="for_guild"):
            call()
    assert eco.for_guild(A).wallet.balance(U1) == INITIAL_BALANCE


# --------------------------- 引き継ぎ --------------------------- #
OLD_SCHEMA = """
CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL, daily_streak INTEGER NOT NULL DEFAULT 0, last_daily TEXT,
    games_played INTEGER NOT NULL DEFAULT 0, total_wagered INTEGER NOT NULL DEFAULT 0, total_won INTEGER NOT NULL DEFAULT 0,
    biggest_win INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, delta INTEGER NOT NULL, balance_after INTEGER NOT NULL,
    reason TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX ledger_user ON ledger (user_id, id);
CREATE TABLE inventory (user_id INTEGER NOT NULL, item_id TEXT NOT NULL, qty INTEGER NOT NULL, PRIMARY KEY (user_id, item_id));
CREATE TABLE profile (user_id INTEGER PRIMARY KEY, title TEXT, card_color TEXT, card_icon TEXT, daily_boost INTEGER NOT NULL DEFAULT 0,
    insurance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE escrow (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount INTEGER NOT NULL, game TEXT NOT NULL, created_at REAL NOT NULL);
"""


def make_old_db(path):
    c = sqlite3.connect(path)
    c.executescript(OLD_SCHEMA)
    c.execute("INSERT INTO users VALUES (1, 3972, 2, '2026-10-25', 5, 600, 700, 400, 1.0)")
    c.execute("INSERT INTO users VALUES (2, 100, 0, NULL, 0, 0, 0, 0, 2.0)")
    c.execute("INSERT INTO ledger (user_id, delta, balance_after, reason, ts) VALUES (1, 3000, 3000, '初期支給', 1.0)")
    c.execute("INSERT INTO inventory VALUES (1, 'title_regular', 1)")
    c.execute("INSERT INTO profile (user_id, title, insurance) VALUES (1, 'title_regular', 2)")
    c.execute("INSERT INTO escrow (user_id, amount, game, created_at) VALUES (2, 50, '古いゲーム', 3.0)")
    c.commit()
    c.close()


def test_old_records_move_to_the_legacy_server(tmp_path):
    path = str(tmp_path / "old.db")
    make_old_db(path)
    eco = open_repositories(path, legacy_guild_id=A, strict=True)
    a, b = eco.for_guild(A), eco.for_guild(B)
    assert (
        a.wallet.balance(U1) == 3972
        and a.wallet.stats(U1)["daily_streak"] == 2
        and a.wallet.stats(U1)["biggest_win"] == 400
    )
    assert (
        a.shop.inventory(U1) == {"title_regular": 1}
        and a.shop.get_profile(U1)["title"] == "title_regular"
        and a.shop.get_profile(U1)["insurance"] == 2
    )
    assert a.wallet.history(U1, 5)[0]["reason"] == "初期支給"
    assert b.wallet.balance(U1) == INITIAL_BALANCE and b.shop.inventory(U1) == {}  # 他のサーバーには引き継がれない
    assert (
        eco.escrow.refund_all_escrows() == 1 and a.wallet.balance(U2) == 150
    )  # 古い預かり金も、引き継ぎ先のサーバーへ返る
    assert os.path.exists(path + ".before-guild-scope")  # 移行前の退避がある


def test_migration_runs_once_and_keeps_data_on_reopen(tmp_path):
    path = str(tmp_path / "old.db")
    make_old_db(path)
    open_repositories(path, legacy_guild_id=A).for_guild(A).wallet.credit(U1, 1, "移行後の操作")
    again = open_repositories(path, legacy_guild_id=B)  # 2回目は、別のIDを渡しても移行しない
    assert again.for_guild(A).wallet.balance(U1) == 3973 and again.for_guild(B).wallet.balance(U1) == INITIAL_BALANCE


def test_migration_requires_the_legacy_server_and_changes_nothing_without_it(tmp_path):
    path = str(tmp_path / "old.db")
    make_old_db(path)
    with pytest.raises(RuntimeError, match="legacy_guild_id"):
        open_repositories(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(users)")}
    assert "guild_id" not in cols  # 何も変わっていない
    assert not os.path.exists(path + ".before-guild-scope")


def test_fresh_database_needs_no_legacy_server(tmp_path):
    eco = open_repositories(str(tmp_path / "new.db"))
    assert eco.for_guild(A).wallet.balance(U1) == INITIAL_BALANCE


# --------------------------- コマンド経由 --------------------------- #
def test_commands_and_games_use_the_servers_own_account(tmp_path):
    """ゲーム・残高・デイリー・ショップを、2つのサーバーから使っても混ざらない（strict の記録で、指定漏れも検知）。"""
    from cogs.casino import simple
    from cogs.economy.cog import EconomyCog

    bot = make_test_bot(str(tmp_path / "t.db"), strict=True, config={"owner_ids": [U1]})
    eco = bot.repos
    deps = make_casino_deps(bot)

    class It:
        def __init__(self, gid, uid=U1):
            self.guild_id, self.guild, self.user = (
                gid,
                SimpleNamespace(id=gid),
                SimpleNamespace(id=uid, display_name="t"),
            )
            self.sent, self.edits = [], []
            self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
            self.followup = SimpleNamespace(send=self._send)

        async def _send(self, *a, **kw):
            self.sent.append(kw)

        async def _defer(self, **kw):
            return None

        async def edit_original_response(self, **kw):
            self.edits.append(kw)

    async def nosleep(*a, **k):
        return None

    import unittest.mock as mock

    with mock.patch("asyncio.sleep", nosleep):
        random.seed(1)
        run(simple.play_roulette(as_interaction(It(A)), deps, 1_000, "red"))  # Aで遊ぶ
        after_a = eco.for_guild(A).wallet.balance(U1)
        assert eco.for_guild(B).wallet.balance(U1) == INITIAL_BALANCE and after_a != INITIAL_BALANCE
        shop_cog = make_shop_cog(bot)
        eco_cog = make_economy_cog(bot)
        it = It(B)
        run(invoke(EconomyCog.daily, eco_cog, it))  # Bでデイリー
        assert (
            eco.for_guild(B).wallet.balance(U1) == INITIAL_BALANCE + DAILY_BASE
            and eco.for_guild(A).wallet.balance(U1) == after_a
        )
        from cogs.shop import actions as shop_actions

        it = It(A)
        run(shop_actions.buy_item(shop_cog, as_interaction(it), "title_rookie", 1))  # Aで称号を買う
        assert eco.for_guild(A).shop.inventory(U1) == {"title_rookie": 1} and eco.for_guild(B).shop.inventory(U1) == {}
        eco.for_guild(B).wallet.credit(U2, 777, "テスト")  # Bに付与 → Bの口座だけ
        assert (
            eco.for_guild(B).wallet.balance(U2) == INITIAL_BALANCE + 777
            and eco.for_guild(A).wallet.peek_stats(U2) is None
        )
        run(invoke(EconomyCog.ranking, eco_cog, It(B)))  # 指定漏れがあれば strict でここで落ちる


# --------------------------- レビュー指摘の追加確認 --------------------------- #
@pytest.mark.parametrize("bad", [0, -5])
def test_for_guild_rejects_invalid_ids(eco, bad):
    with pytest.raises(ValueError):
        eco.for_guild(bad)
    assert eco.db.conn.execute("SELECT COUNT(*) FROM accounts WHERE guild_id=0").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_legacy_id_is_rejected_before_any_change(tmp_path, bad):
    path = str(tmp_path / "old.db")
    make_old_db(path)
    with pytest.raises(ValueError):
        open_repositories(path, legacy_guild_id=bad)
    assert "guild_id" not in {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(users)")}
    assert not os.path.exists(path + ".before-guild-scope")


def test_migration_accepts_str_id_and_old_db_without_optional_tables(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE users (user_id INTEGER PRIMARY KEY, balance INTEGER NOT NULL, daily_streak INTEGER NOT NULL DEFAULT 0,
        last_daily TEXT, games_played INTEGER NOT NULL DEFAULT 0, total_wagered INTEGER NOT NULL DEFAULT 0,
        total_won INTEGER NOT NULL DEFAULT 0, biggest_win INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
        INSERT INTO users VALUES (1, 777, 0, NULL, 0, 0, 0, 0, 1.0);""")
    c.commit()
    c.close()
    eco = open_repositories(
        path,
        legacy_guild_id=cast(int, str(A)),
        strict=True,  # config.json の文字列の ID も受け付ける
    )  # inventory / profile / ledger / escrow が無い古い記録でも移行できる
    assert eco.for_guild(A).wallet.balance(1) == 777 and eco.for_guild(B).wallet.balance(1) == INITIAL_BALANCE


def test_refund_skips_escrow_with_unknown_server(eco):
    eco.db.conn.execute("INSERT INTO escrow (guild_id, user_id, amount, game, created_at) VALUES (0, 9, 50, 'x', 1.0)")
    eco.db.conn.execute(
        "INSERT INTO escrow (guild_id, user_id, amount, game, created_at) VALUES (?, ?, 50, 'x', 1.0)", (A, U1)
    )
    eco.db.conn.commit()
    assert eco.escrow.refund_all_escrows() == 2
    assert eco.for_guild(A).wallet.balance(U1) == INITIAL_BALANCE + 50
