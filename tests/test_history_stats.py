"""履歴の1ゲーム1行表示、成績（勝率・損益）、売るの半分/全部、チンチロ・BJ の「もう一度」。"""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from cogs.economy_cog import EconomyCog
from cogs.market_cog import MarketCog
from core import history
from core.economy import Economy
from core.session import Sessions

pytestmark = pytest.mark.real_scope

G, U = 10, 1


def run(coro):
    return asyncio.run(coro)


def row(delta, reason, bal, ts=1_000_000):
    return {"delta": delta, "reason": reason, "balance_after": bal, "ts": ts}


# ----------------------------- 履歴 ----------------------------- #
def test_one_game_is_one_line_with_net_and_final_balance():
    rows = [row(95 + 100, "払い戻し:コイン", 3195), row(-100, "賭け:コイン", 3000)]          # 新しい順
    lines = history.format_history(rows)
    assert len(lines) == 1 and "コイン 賭100" in lines[0] and "**+95**" in lines[0] and "3,195ペリカ" in lines[0]


def test_losing_game_without_payout_row_and_double_down_and_penalty():
    assert history.format_history([row(-100, "賭け:コイン", 2900)])[0].count("**-100**") == 1
    bj = [row(400, "払い戻し:ブラックジャック", 3400), row(-200, "追加の賭け:ブラックジャック", 3000), row(-200, "賭け:ブラックジャック", 3200)]
    line = history.format_history(bj)[0]
    assert "賭400" in line and "**+0**" in line                                            # 賭け金は追加分も合計、損益は差し引き
    chin = [row(-300, "追加の支払い:チンチロ", 2400), row(-100, "賭け:チンチロ", 2700)]
    line = history.format_history(chin)[0]
    assert "賭100" in line and "**-400**" in line and len(history.format_history(chin)) == 1


def test_transfers_show_the_person_and_other_rows_are_kept():
    rows = [row(-1100, "送金（送）→42", 1900), row(500, "送金（受）←7", 3000), row(1000, "デイリーボーナス", 2500)]
    lines = history.format_history(rows)
    assert "送金 → <@42>" in lines[0] and "送金 ← <@7>" in lines[1] and "デイリーボーナス" in lines[2]


def test_consecutive_games_do_not_merge_and_limit_applies():
    rows = []
    for i in range(20):
        rows += [row(100, "払い戻し:コイン", 1000 + i), row(-50, "賭け:コイン", 900 + i)]
    assert len(history.format_history(rows, 12)) == 12 and all(l.count("コイン 賭50") == 1 for l in history.format_history(rows, 12))


def test_history_command_shows_merged_lines(tmp_path):
    eco = Economy(str(tmp_path / "t.db"), strict=True)
    a = eco.for_guild(G)
    esc = a.place_bet(U, 100, "コイン")
    a.settle(esc, 195, "コイン")
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={})
    cog = EconomyCog(bot)
    sent = []

    class It:
        user = SimpleNamespace(id=U)
        guild = SimpleNamespace(id=G)
        response = SimpleNamespace(send_message=None)
    it = It()

    async def send(*a, **kw):
        sent.append(kw)
    it.response = SimpleNamespace(send_message=send)
    run(EconomyCog.history.callback(cog, it))
    text = sent[0]["embed"].description
    assert "コイン 賭100" in text and "**+95**" in text and "賭け:" not in text


# ----------------------------- 成績 ----------------------------- #
def test_play_summary_counts_plays_wins_net_and_best_streak(tmp_path):
    a = Economy(str(tmp_path / "t.db"), strict=True).for_guild(G)
    day = datetime.date(2026, 10, 30)
    for net in (100, 100, -100, 0, 50):
        a.record_play(U, "コイン", 100, net, day)
    s = a.play_summary(U)
    assert s == {"plays": 5, "wins": 3, "net_total": 150, "best_streak": 2}
    assert a.play_summary(999) == {"plays": 0, "wins": 0, "net_total": 0, "best_streak": 0}


def test_balance_card_shows_win_rate_profit_and_best_streak(tmp_path):
    eco = Economy(str(tmp_path / "t.db"), strict=True)
    a = eco.for_guild(G)
    day = datetime.date(2026, 10, 30)
    a.balance(U)
    for net in (100, 100, -100, 100):
        a.record_play(U, "コイン", 100, net, day)
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={})
    cog = EconomyCog(bot)
    sent = []

    async def send(*args, **kw):
        sent.append(kw)
    member = SimpleNamespace(id=U, display_name="テスト", display_avatar=SimpleNamespace(url="https://example.com/a.png"))
    it = SimpleNamespace(user=member, guild=SimpleNamespace(id=G), response=SimpleNamespace(send_message=send))
    run(EconomyCog.balance.callback(cog, it, None))
    fields = {f.name: f.value for f in sent[0]["embed"].fields}
    assert fields["勝率"] == "75%（4戦）" and fields["ゲームの損益"] == "+200ペリカ" and fields["最大連勝"] == "2連勝"
    assert fields["連続ログイン"] == "まだ受け取っていません"


def test_play_state_without_summary_columns_is_upgraded(tmp_path):
    path = str(tmp_path / "old.db")
    e = Economy(path, strict=True)
    e._conn.execute("DROP TABLE play_state")
    e._conn.execute("CREATE TABLE play_state (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, win_streak INTEGER NOT NULL DEFAULT 0, "
                    "best_streak INTEGER NOT NULL DEFAULT 0, day_streak INTEGER NOT NULL DEFAULT 0, streak_day TEXT, PRIMARY KEY (guild_id, user_id))")
    e._conn.execute("INSERT INTO play_state VALUES (10, 1, 2, 5, 0, NULL)")
    e._conn.commit()
    e._conn.close()
    again = Economy(path, strict=True).for_guild(G)
    assert again.play_summary(U)["best_streak"] == 5 and again.play_summary(U)["plays"] == 0


# ----------------------------- 売る: 半分/全部 ----------------------------- #
class It:
    def __init__(self):
        self.user = SimpleNamespace(id=U, display_name="テスト")
        self.guild = SimpleNamespace(id=G)
        self.guild_id = G
        self.sent = []
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self.sent.append(kw)

    async def _defer(self, **kw):
        return None


@pytest.fixture
def cog(tmp_path):
    bot = SimpleNamespace(economy=Economy(str(tmp_path / "t.db"), strict=True), sessions=Sessions(cooldown=0), config={})
    c = MarketCog(bot)
    c._price()
    return c


def test_sell_half_then_all_and_both_options_are_refused(cog):
    from discord import app_commands
    run(MarketCog.buy.callback(cog, It(), 2_000))
    have = cog.eco.for_guild(G).crypto_holding(U)[0]
    half = app_commands.Choice(name="半分", value="half")
    allc = app_commands.Choice(name="全部", value="all")
    it = It()
    run(MarketCog.sell.callback(cog, it, 1.0, half))
    assert "どちらか1つ" in it.sent[-1]["embed"].title and cog.eco.for_guild(G).crypto_holding(U)[0] == have
    run(MarketCog.sell.callback(cog, It(), None, half))
    assert cog.eco.for_guild(G).crypto_holding(U)[0] == have - have // 2
    run(MarketCog.sell.callback(cog, It(), None, allc))
    assert cog.eco.for_guild(G).crypto_holding(U) == (0, 0)


# ----------------------------- 総資産ランキング ----------------------------- #
def test_ranking_counts_crypto_value_so_buying_does_not_drop_the_rank(tmp_path):
    eco = Economy(str(tmp_path / "t.db"), strict=True)
    a = eco.for_guild(G)
    eco.market_set(1.0, 1000.0, 0)
    a.credit(1, 7_000, "テスト")                    # 現金 10,000
    a.credit(2, 5_000, "テスト")                    # 現金 8,000
    assert [u for _, u, _ in a.top(10)] == [1, 2]
    a.crypto_buy(1, 9_000, 1000.0)                  # 1番の人が、ほぼ全額をトークンに替える
    assert [(r, u) for r, u, _ in a.top(10)] == [(1, 1), (2, 2)]              # 順位は変わらない
    assert a.top(10)[0][2] == 10_000 and a.rank_of(1)[1] == 10_000            # 総資産は売買前と同じ（手数料なし）
    eco.market_set(2.0, 1500.0, 0)                  # 価格が上がれば、総資産も増える
    assert a.top(10)[0][2] > 10_000
    eco.market_set(3.0, 100.0, 0)                   # 暴落すれば、順位も下がる
    assert [u for _, u, _ in a.top(10)] == [2, 1]


def test_ranking_embed_shows_total_and_crypto_part(tmp_path):
    from core import ranking
    eco = Economy(str(tmp_path / "t.db"), strict=True)
    a = eco.for_guild(G)
    eco.market_set(1.0, 1000.0, 0)
    a.crypto_buy(1, 2_000, 1000.0)
    text = ranking.build_embed(a, None, "t").description
    assert "うちCS" in text and "3,000ペリカ" in text


def test_daily_is_1000_every_6_hours_and_next_time_is_stated(tmp_path, monkeypatch):
    import datetime as dt
    from cogs import economy_cog
    assert Economy.DAILY_BASE == 1000 and economy_cog.DAILY_SLOT_HOURS == 6
    eco = Economy(str(tmp_path / "t.db"), strict=True)
    bot = SimpleNamespace(economy=eco, sessions=Sessions(cooldown=0), config={})
    cog = EconomyCog(bot)
    clock = {"now": dt.datetime(2026, 10, 30, 7, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))}
    monkeypatch.setattr(economy_cog, "local_now", lambda b: clock["now"])
    sent = []

    async def send(*a, **kw):
        sent.append(kw)
    it = SimpleNamespace(user=SimpleNamespace(id=U), guild=SimpleNamespace(id=G), response=SimpleNamespace(send_message=send))
    run(EconomyCog.daily.callback(cog, it))                                       # 7:30（6時〜の時間帯）
    first = sent[-1]["embed"].description
    assert "+1,000ペリカ" in first and "12:00（日本時間）" in first
    run(EconomyCog.daily.callback(cog, it))                                       # 同じ時間帯は受け取れない
    assert "まだ受け取れません" in sent[-1]["embed"].title and "12:00（日本時間）" in sent[-1]["embed"].description
    clock["now"] = clock["now"].replace(hour=12, minute=0)
    run(EconomyCog.daily.callback(cog, it))                                       # 12:00になれば、また受け取れる
    assert "+1,000ペリカ" in sent[-1]["embed"].description and "18:00（日本時間）" in sent[-1]["embed"].description
    clock["now"] = clock["now"].replace(hour=18, minute=59)
    run(EconomyCog.daily.callback(cog, it))
    assert "0:00（日本時間）" in sent[-1]["embed"].description                    # 18時の次は、翌日の0:00


def test_daily_slots_in_economy_keep_streak_by_day(tmp_path):
    import datetime as dt
    a = Economy(str(tmp_path / "t.db"), strict=True).for_guild(G)
    day = dt.date(2026, 10, 30)
    r1 = a.claim_daily(U, day, day.toordinal() * 4 + 1)
    r2 = a.claim_daily(U, day, day.toordinal() * 4 + 2)
    assert r1.ok and r2.ok and r1.streak == r2.streak == 1 and r2.amount == Economy.DAILY_BASE          # 同じ日は連続日数が増えない
    assert not a.claim_daily(U, day, day.toordinal() * 4 + 2).ok                                         # 同じ時間帯は2回目不可
    assert not a.claim_daily(U, day, day.toordinal() * 4 + 1).ok                                         # 古い時間帯にも戻れない
    nxt = day + dt.timedelta(days=1)
    r3 = a.claim_daily(U, nxt, nxt.toordinal() * 4)
    assert r3.ok and r3.streak == 2 and r3.amount == Economy.DAILY_BASE + Economy.DAILY_STREAK_BONUS
    far = nxt + dt.timedelta(days=3)
    assert a.claim_daily(U, far, far.toordinal() * 4).streak == 1                                        # 日が空けば連続は途切れる


def test_users_table_without_last_slot_is_upgraded_and_old_claim_counts_as_that_day(tmp_path):
    import datetime as dt
    path = str(tmp_path / "t.db")
    e = Economy(path, strict=True)
    a = e.for_guild(G)
    day = dt.date(2026, 10, 30)
    a.claim_daily(U, day)                                                          # 旧方式（1日1回）で受け取る
    e._conn.execute("UPDATE users SET last_slot=NULL")                            # 時間帯を記録する前の状態にする
    e._conn.commit()
    again = Economy(path, strict=True).for_guild(G)
    assert not again.claim_daily(U, day).ok                                        # 旧方式のままなら、同じ日は不可
    assert again.claim_daily(U, day, day.toordinal() * 4 + 3).ok                   # 時間帯方式では、その日の2回目として受け取れる
