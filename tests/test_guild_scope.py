"""ペリカの記録がサーバーごとに別々であること、古い記録の引き継ぎ。"""

import asyncio
import datetime
import os
import random
import sqlite3
from types import SimpleNamespace

import pytest

from core import shop
from core.economy import INITIAL_BALANCE, Economy

pytestmark = pytest.mark.real_scope       # この中は、本物の for_guild（サーバーごとに分かれる）で確かめる

A, B = 10, 20
U1, U2 = 1, 2


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def eco(tmp_path):
    return Economy(str(tmp_path / "t.db"), strict=True)


# --------------------------- 分離 --------------------------- #
def test_each_server_has_its_own_balance_and_initial_grant(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    assert a.balance(U1) == INITIAL_BALANCE and b.balance(U1) == INITIAL_BALANCE      # サーバーごとに初期支給
    a.credit(U1, 5_000, "テスト")
    assert a.balance(U1) == INITIAL_BALANCE + 5_000 and b.balance(U1) == INITIAL_BALANCE


def test_daily_streak_and_claim_are_per_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    day = datetime.date(2026, 10, 20)
    assert a.claim_daily(U1, day).ok
    assert not a.claim_daily(U1, day).ok                       # Aではもう受け取り済み
    assert b.claim_daily(U1, day).ok                           # Bではまだ受け取れる
    assert a.claim_daily(U1, day + datetime.timedelta(days=1)).streak == 2 and b.stats(U1)["daily_streak"] == 1


def test_ranking_only_counts_this_servers_balances(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    a.credit(U1, 1_000, "テスト")
    b.credit(U1, 90_000, "テスト")                              # Bで大金持ちでも、Aのランキングには影響しない
    b.credit(U2, 10, "テスト")
    assert [(r, u, bal) for r, u, bal in a.top(10)] == [(1, U1, INITIAL_BALANCE + 1_000)]
    assert [u for _, u, _ in b.top(10)] == [U1, U2]
    assert a.rank_of(U2) is None and b.rank_of(U2)[0] == 2


def test_history_and_stats_are_per_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    a.credit(U1, 500, "Aだけ")
    assert any(h["reason"] == "Aだけ" for h in a.history(U1, 10))
    assert not any(h["reason"] == "Aだけ" for h in b.history(U1, 10))
    eid = a.place_bet(U1, 100, "テスト")
    a.settle(eid, 300, "テスト")
    assert a.stats(U1)["games_played"] == 1 and b.stats(U1)["games_played"] == 0


def test_shop_items_and_equipment_are_per_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    it = shop.BY_ID["title_rookie"]
    assert a.purchase(U1, it.id, shop.label(it), it.price, True).ok
    a.equip(U1, "title", it.id)
    assert a.inventory(U1) == {it.id: 1} and a.get_profile(U1)["title"] == it.id
    assert b.inventory(U1) == {} and b.get_profile(U1)["title"] is None               # Bには持ち込めない
    assert not b.equip(U1, "title", it.id)                                              # Bでは装備もできない
    assert b.balance(U1) == INITIAL_BALANCE                                              # Aでの買い物はBの残高に響かない


def test_consumables_are_per_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    for e in (a, b):
        e.credit(U1, 10_000, "テスト")
        e.purchase(U1, shop.INSURANCE, "保険券", 2_000, False)
    assert a.arm(U1, shop.INSURANCE)
    eid_b = b.place_bet(U1, 1_000, "テスト")
    assert b.settle_with_penalty(eid_b, 0, "テスト", 0)[2] == 0                          # Bでは保険は効かない（Aで使った券）
    eid_a = a.place_bet(U1, 1_000, "テスト")
    assert a.settle_with_penalty(eid_a, 0, "テスト", 0)[2] == 500


def test_transfer_stays_inside_one_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    for _ in range(3):
        a.settle(a.place_bet(U1, 10, "テスト"), 10, "テスト")
    res = a.transfer(U1, U2, 500, 0.1, 20_000, 0.0, 3)
    assert res.ok
    assert a.balance(U2) == INITIAL_BALANCE + 500 and b.balance(U2) == INITIAL_BALANCE   # 別サーバーの口座は動かない
    assert not b.transfer(U1, U2, 100, 0.1, 20_000, 0.0, 3).ok                           # Bでは、まだ3回遊んでいない


def test_escrow_cannot_be_settled_from_another_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    eid = a.place_bet(U1, 1_000, "テスト")
    assert b.escrow_amount(eid) == 0
    assert b.settle(eid, 999_999, "不正") is None and b.refund(eid) is False             # 他サーバーの預かり金は触れない
    assert a.escrow_amount(eid) == 1_000
    assert a.settle(eid, 0, "テスト") == -1_000


def test_refund_on_startup_goes_back_to_the_right_server(eco):
    a, b = eco.for_guild(A), eco.for_guild(B)
    a.place_bet(U1, 700, "A")
    b.place_bet(U1, 400, "B")
    assert eco.refund_all_escrows() == 2
    assert a.balance(U1) == INITIAL_BALANCE and b.balance(U1) == INITIAL_BALANCE


def test_strict_economy_refuses_unscoped_use(eco):
    for call in (lambda: eco.balance(U1), lambda: eco.top(10), lambda: eco.claim_daily(U1, datetime.date.today())):
        with pytest.raises(RuntimeError, match="for_guild"):
            call()
    assert eco.for_guild(A).balance(U1) == INITIAL_BALANCE


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
    eco = Economy(path, legacy_guild_id=A, strict=True)
    a, b = eco.for_guild(A), eco.for_guild(B)
    assert a.balance(U1) == 3972 and a.stats(U1)["daily_streak"] == 2 and a.stats(U1)["biggest_win"] == 400
    assert a.inventory(U1) == {"title_regular": 1} and a.get_profile(U1)["title"] == "title_regular" and a.get_profile(U1)["insurance"] == 2
    assert a.history(U1, 5)[0]["reason"] == "初期支給"
    assert b.balance(U1) == INITIAL_BALANCE and b.inventory(U1) == {}                  # 他のサーバーには引き継がれない
    assert eco.refund_all_escrows() == 1 and a.balance(U2) == 150                       # 古い預かり金も、引き継ぎ先のサーバーへ返る
    assert os.path.exists(path + ".before-guild-scope")                                 # 移行前の退避がある


def test_migration_runs_once_and_keeps_data_on_reopen(tmp_path):
    path = str(tmp_path / "old.db")
    make_old_db(path)
    Economy(path, legacy_guild_id=A).for_guild(A).credit(U1, 1, "移行後の操作")
    again = Economy(path, legacy_guild_id=B)                                           # 2回目は、別のIDを渡しても移行しない
    assert again.for_guild(A).balance(U1) == 3973 and again.for_guild(B).balance(U1) == INITIAL_BALANCE


def test_migration_requires_the_legacy_server_and_changes_nothing_without_it(tmp_path):
    path = str(tmp_path / "old.db")
    make_old_db(path)
    with pytest.raises(RuntimeError, match="legacy_guild_id"):
        Economy(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(users)")}
    assert "guild_id" not in cols                                                        # 何も変わっていない
    assert not os.path.exists(path + ".before-guild-scope")


def test_fresh_database_needs_no_legacy_server(tmp_path):
    eco = Economy(str(tmp_path / "new.db"))
    assert eco.for_guild(A).balance(U1) == INITIAL_BALANCE


# --------------------------- コマンド経由 --------------------------- #
def test_commands_and_games_use_the_servers_own_account(tmp_path):
    """ゲーム・残高・デイリー・ショップを、2つのサーバーから使っても混ざらない（strict の記録で、指定漏れも検知）。"""
    from cogs.economy_cog import EconomyCog
    from cogs.shop_cog import ShopCog
    from core.session import Sessions
    from play import simple

    eco = Economy(str(tmp_path / "t.db"), strict=True)
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={"owner_ids": [U1]})

    class It:
        def __init__(self, gid, uid=U1):
            self.guild_id, self.guild, self.user = gid, SimpleNamespace(id=gid), SimpleNamespace(id=uid, display_name="t")
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
        run(simple.play_coin(It(A), bot, 1_000, "表"))                                 # Aで遊ぶ
        after_a = eco.for_guild(A).balance(U1)
        assert eco.for_guild(B).balance(U1) == INITIAL_BALANCE and after_a != INITIAL_BALANCE
        eco_cog, shop_cog = EconomyCog(bot), ShopCog(bot)
        it = It(B)
        run(EconomyCog.daily.callback(eco_cog, it))                                    # Bでデイリー
        assert eco.for_guild(B).balance(U1) == INITIAL_BALANCE + 1_000 and eco.for_guild(A).balance(U1) == after_a
        it = It(A)
        run(ShopCog.buy.callback(shop_cog, it, "title_rookie", 1))                     # Aで称号を買う
        assert eco.for_guild(A).inventory(U1) == {"title_rookie": 1} and eco.for_guild(B).inventory(U1) == {}
        it = It(B)
        run(EconomyCog.grant.callback(eco_cog, it, SimpleNamespace(id=U2, mention="@x"), 777))      # Bで付与 → Bの口座だけ
        assert eco.for_guild(B).balance(U2) == INITIAL_BALANCE + 777 and eco.for_guild(A).peek_stats(U2) is None
        run(EconomyCog.ranking.callback(eco_cog, It(B)))                                # 指定漏れがあれば strict でここで落ちる


# --------------------------- レビュー指摘の追加確認 --------------------------- #
@pytest.mark.parametrize("bad", [0, -5])
def test_for_guild_rejects_invalid_ids(eco, bad):
    with pytest.raises(ValueError):
        eco.for_guild(bad)
    assert eco._conn.execute("SELECT COUNT(*) FROM users WHERE guild_id=0").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_legacy_id_is_rejected_before_any_change(tmp_path, bad):
    path = str(tmp_path / "old.db")
    make_old_db(path)
    with pytest.raises(ValueError):
        Economy(path, legacy_guild_id=bad)
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
    eco = Economy(path, legacy_guild_id=str(A), strict=True)       # inventory / profile / ledger / escrow が無い古い記録でも移行できる
    assert eco.for_guild(A).balance(1) == 777 and eco.for_guild(B).balance(1) == INITIAL_BALANCE


def test_refund_skips_escrow_with_unknown_server(eco):
    eco._conn.execute("INSERT INTO escrow (guild_id, user_id, amount, game, created_at) VALUES (0, 9, 50, 'x', 1.0)")
    eco._conn.execute("INSERT INTO escrow (guild_id, user_id, amount, game, created_at) VALUES (?, ?, 50, 'x', 1.0)", (A, U1))
    eco._conn.commit()
    assert eco.refund_all_escrows() == 2
    assert eco.for_guild(A).balance(U1) == INITIAL_BALANCE + 50
