"""証券の複数銘柄: 相関のある値動き・銘柄ごとの保有・旧データの移行・コマンド構成。"""

import asyncio
import math
import random
import time
from types import SimpleNamespace

import pytest
from discord import app_commands

from cogs.market import orders
from cogs.market.cog import MarketCog
from domain import market
from domain.types import INITIAL_BALANCE
from infra.db.repositories import open_repositories
from tests.fakes import as_interaction, buttons, invoke, must, selects
from tests.support import make_market_cog, make_test_bot

pytestmark = pytest.mark.real_scope

G, U = 10, 1


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"), strict=True)


def _start():
    return {s.key: s.start for s in market.SYMBOLS}


# ----------------------------- 相関のある値動き ----------------------------- #
def test_cholesky_reproduces_the_covariance_matrix():
    low, n = market.CHOLESKY, len(market.SYMBOLS)
    for i in range(n):
        for j in range(n):
            assert sum(low[i][k] * low[j][k] for k in range(n)) == pytest.approx(market.COVARIANCE[i][j], abs=1e-12)


def test_cholesky_rejects_an_impossible_correlation():
    with pytest.raises(ValueError):
        market.cholesky([[1.0, 2.0], [2.0, 1.0]])  # 相関が1を超える


def test_correlation_is_symmetric_and_defined_for_every_pair():
    for a in market.SYMBOLS:
        assert market.correlation(a.key, a.key) == 1.0
        for b in market.SYMBOLS:
            assert market.correlation(a.key, b.key) == market.correlation(b.key, a.key)


def test_simulated_returns_match_the_target_correlation_without_panic(monkeypatch):
    monkeypatch.setattr(market, "EVENT_PROB", 0.0)  # 有事を切って、通常の相関だけを見る
    rng, n = random.Random(1), 60_000
    prices, series = _start(), {k: [] for k in prices_keys()}
    for _ in range(n):
        new, _ = market.next_prices(prices, rng)
        for k in new:
            series[k].append(math.log(new[k] / prices[k]))
        prices = {k: v if 100 < v < 10_000 else market.BY_KEY[k].start for k, v in new.items()}
    for a, b in (("cs", "teio"), ("cs", "gold"), ("teio", "gold")):
        want = market.correlation(a, b)
        assert _corr(series[a], series[b]) == pytest.approx(want, abs=0.02)


def prices_keys():
    return [s.key for s in market.SYMBOLS]


def _corr(x, y):
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((p - mx) * (q - my) for p, q in zip(x, y, strict=True))
    return sxy / math.sqrt(sum((p - mx) ** 2 for p in x) * sum((q - my) ** 2 for q in y))


def test_panic_moves_every_symbol_the_same_way_scaled_by_volatility(monkeypatch):
    monkeypatch.setattr(market, "EVENT_PROB", 1.0)
    for seed in range(40):
        rng = random.Random(seed)
        new, event = market.next_prices(_start(), rng)
        assert event in (-1, 1)
        for s in market.SYMBOLS:
            moved = new[s.key] / s.start - 1
            if abs(moved) > 0.5 * market.EVENT_RANGE[0]:  # 通常の揺れより十分大きいとき、向きは有事の向き
                assert (moved > 0) == (event > 0), (seed, s.key)
        assert new["numa"] / market.NUMA.start != new["cs"] / market.CS.start


# ----------------------------- 相関の表示 ----------------------------- #
def test_correlation_from_history_needs_enough_aligned_points():
    hist = [(float(t), 1000 + t % 7, 0) for t in range(10)]
    assert market.correlation_from_history(hist, hist) is None  # 少なすぎる
    long = [(float(t), 1000 * math.exp(0.01 * math.sin(t)), 0) for t in range(120)]
    assert market.correlation_from_history(long, long) == pytest.approx(1.0)
    inverse = [(t, 1e6 / p, e) for t, p, e in long]
    assert market.correlation_from_history(long, inverse) == pytest.approx(-1.0)
    flat = [(float(t), 500.0, 0) for t in range(120)]
    assert market.correlation_from_history(long, flat) is None  # 動かない銘柄とは求まらない


@pytest.mark.parametrize(
    "value,word", [(0.85, "強く連動"), (0.4, "やや連動"), (0.0, "ほぼ無関係"), (-0.4, "やや逆"), (-0.7, "逆に動く")]
)
def test_describe_correlation_words(value, word):
    assert word in market.describe_correlation(value)
    assert market.describe_correlation(None) == "—"


# ----------------------------- 銘柄ごとの保有・価格・空売り ----------------------------- #
def test_prices_history_and_holdings_are_kept_per_symbol(eco):
    eco.market.market_set(1.0, 1100, 0, "cs")
    eco.market.market_set(1.0, 1300, 0, "teio")
    assert eco.market.market_latest("cs")[1] == 1100 and eco.market.market_latest("teio")[1] == 1300
    assert eco.market.market_latest("gold") is None and eco.market.latest_prices() == {"cs": 1100, "teio": 1300}
    a = eco.for_guild(G)
    a.crypto.crypto_buy(U, 1_000, 1100, "cs")
    a.crypto.crypto_buy(U, 1_500, 1300, "teio")
    assert [s for s, _, _ in a.crypto.holdings(U)] == ["cs", "teio"]
    assert a.crypto.crypto_holding(U, "gold") == (0, 0)
    assert not a.crypto.crypto_sell(U, 1, 1000, "gold").ok  # 持っていない銘柄は売れない
    assert a.crypto.crypto_sell(U, None, 1300, "teio").ok and [s for s, _, _ in a.crypto.holdings(U)] == ["cs"]
    assert [u for u, _, _ in a.crypto.crypto_holders(symbol="teio")] == []


def test_shorts_are_kept_per_symbol_and_chain_losscut_closes_the_biggest_loser_first(eco):
    a = eco.for_guild(G)
    assert a.margin.short_open(U, 10_000, 100.0, 5, "cs").ok and a.margin.short_open(U, 10_000, 100.0, 5, "gold").ok
    assert [s for s, *_ in a.margin.short_positions(U)] == ["cs", "gold"]
    done = eco.margin.margin_losscut(
        {"cs": 100.0, "gold": 160.0}
    )  # goldだけ +60%: 損失600が担保200を超え、維持率が割れる
    assert done == [(G, U, "gold", "short", 600)]  # 損の大きい gold だけを切れば足りる（csは残る）
    assert [s for s, *_ in a.margin.short_positions(U)] == ["cs"]
    assert (
        a.wallet.balance(U) == INITIAL_BALANCE - 400 - 400 and a.debt.debt(U) == 0
    )  # 担保を超えた400は、現金から取り立て
    eco.market.market_set(1.0, 100.0, 0, "cs")
    assert a.margin.margin_equity()[U] == 200  # 残った CS の建玉の担保だけ（gold は決済済み）


def test_total_assets_count_every_symbol(eco):
    a = eco.for_guild(G)
    for k, p in (("cs", 1000.0), ("teio", 1200.0)):
        eco.market.market_set(1.0, p, 0, k)
    r1, r2 = a.crypto.crypto_buy(U, 1_000, 1000.0, "cs"), a.crypto.crypto_buy(U, 1_500, 1200.0, "teio")
    eco.market.market_set(2.0, 1100.0, 0, "cs")
    eco.market.market_set(2.0, 1200.0, 0, "teio")
    worth = a.crypto.crypto_values()[U]
    assert worth == market.sell_quote(r1.units, 1100.0)[0] + market.sell_quote(r2.units, 1200.0)[0]


# ----------------------------- 旧データの移行 ----------------------------- #
def test_old_single_symbol_tables_become_cs_without_losing_rows(tmp_path):
    path = str(tmp_path / "old.db")
    first = open_repositories(path, strict=True)
    c = first.db.conn
    for t in ("market_prices", "crypto", "crypto_short", "crypto_long", "margin_positions"):
        c.execute(f"DROP TABLE IF EXISTS {t}")
    c.execute("CREATE TABLE market_prices (ts REAL PRIMARY KEY, price REAL NOT NULL, event INTEGER NOT NULL DEFAULT 0)")
    c.execute(
        "CREATE TABLE crypto (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, units INTEGER NOT NULL DEFAULT 0,"
        " cost INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))"
    )
    c.execute(
        "CREATE TABLE crypto_short (guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, units INTEGER NOT NULL DEFAULT 0,"
        " entry_value REAL NOT NULL DEFAULT 0, margin INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, user_id))"
    )
    c.execute("INSERT INTO market_prices VALUES (5.0, 1234.0, 1)")
    c.execute("INSERT INTO crypto VALUES (?, ?, 7000, 7000)", (G, U))
    c.execute("INSERT INTO crypto_short VALUES (?, ?, 2000, 2000.0, 1000)", (G, U))
    c.execute("DROP TABLE chart_posts")
    c.execute(
        "CREATE TABLE chart_posts (channel_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, guild_id INTEGER NOT NULL DEFAULT 0, period TEXT NOT NULL DEFAULT '24h', ts REAL NOT NULL DEFAULT 0)"
    )
    c.execute("INSERT INTO chart_posts (channel_id, message_id) VALUES (5, 99)")
    c.execute("PRAGMA user_version = 0")
    c.commit()
    c.close()
    again = open_repositories(path, strict=True)
    a = again.for_guild(G)
    assert again.market.market_latest("cs") == (5.0, 1234.0, 1) or tuple(must(again.market.market_latest("cs"))) == (
        5.0,
        1234.0,
        1,
    )
    assert a.crypto.crypto_holding(U) == (7000, 7000) and a.margin.short_position(U) == (2000, 2000.0, 1000)
    assert a.crypto.holdings(U) == [("cs", 7000, 7000)]
    assert again.market.live_chart_posts()[0]["symbol"] == "cs"
    again.db.conn.close()
    third = open_repositories(path, strict=True)  # 2回目の起動でも壊れない（移行は1回だけ）
    assert third.for_guild(G).crypto.crypto_holding(U) == (7000, 7000)
    assert third.db.conn.execute("SELECT COUNT(*) FROM market_prices").fetchone()[0] == 1


# ----------------------------- コマンド ----------------------------- #
def test_securities_group_has_four_commands_and_order_is_split_by_kind():
    assert {c.name for c in MarketCog.group.commands} == {"注文", "保有", "市況", "決済"}
    assert {c.name for c in MarketCog.order_group.commands} == {
        "現物",
        "信用",
        "コール",
        "プット",
        "ペア",
        "裸売り",
        "債券",
    }
    for command in MarketCog.order_group.commands:  # 債券は銘柄の選択肢に混ざらない(#79)
        assert isinstance(command, app_commands.Command)
        symbol = next((p for p in command.parameters if p.display_name == "銘柄"), None)
        assert symbol is None or [c.value for c in symbol.choices] == prices_keys(), command.name
    assert [c.value for c in MarketCog.order_bond._params["kind"].choices] == ["normal", "junk"]
    assert [c.value for c in MarketCog.board._params["symbol"].choices] == prices_keys()


def make_cog(eco):
    bot = make_test_bot(repos=eco)
    cog = make_market_cog(bot)
    cog._market_svc.latest_prices()
    return cog


def test_buy_other_symbol_via_command_and_holdings_list_it(eco):
    from tests.domain.test_market import It  # 同じ疑似インタラクションを使う

    cog = make_cog(eco)
    it = It()
    asyncio.run(orders.buy_spot(cog, as_interaction(it), "gold", 2_000))
    assert "GOLD地金" in it.sent[-1]["embed"].title
    assert eco.for_guild(G).crypto.crypto_holding(U, "gold")[0] > 0 and eco.for_guild(G).crypto.crypto_holding(
        U, "cs"
    ) == (0, 0)
    it = It()
    asyncio.run(invoke(MarketCog.holdings, cog, it))
    assert "GOLD地金" in it.sent[-1]["embed"].description
    assert eco.for_guild(G).wallet.balance(U) < INITIAL_BALANCE


def test_step_prices_records_every_symbol_at_the_same_time(eco):
    bot = make_test_bot(repos=eco)
    cog = make_market_cog(bot)
    new, _ = cog.step_prices()
    assert set(new) == set(prices_keys())
    stamps = {eco.market.market_latest(k)[0] for k in new}
    assert len(stamps) == 1 and eco.market.latest_prices() == new


# ----------------------------- 注文・保有・市況・決済 ----------------------------- #
def choice(value):
    return SimpleNamespace(value=value)


def test_order_subcommands_buy_spot_margin_and_bond(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    eco.for_guild(G).wallet.credit(U, 20_000, "テスト")
    it = It()
    asyncio.run(invoke(MarketCog.order_spot, cog, it, choice("teio"), 2_000))
    assert eco.for_guild(G).crypto.crypto_holding(U, "teio")[0] > 0
    it = It()
    asyncio.run(invoke(MarketCog.order_margin, cog, it, choice("gold"), choice("short"), 1_000, choice(2)))
    assert eco.for_guild(G).margin.short_position(U, "gold")[0] > 0 and "空売り" in it.sent[-1]["embed"].title
    it = It()
    asyncio.run(invoke(MarketCog.order_bond, cog, it, choice("normal"), 1_000))
    assert len(eco.for_guild(G).bonds.bond_list(U)["active"]) == 1 and "通常債" in it.sent[-1]["embed"].title


def test_spot_amount_suggestions_stop_at_the_spot_limit(eco):
    cog = make_cog(eco)
    eco.for_guild(G).wallet.set_balance(U, 3_000_000, "テスト")
    it = SimpleNamespace(guild_id=G, user=SimpleNamespace(id=U))
    spot = asyncio.run(cog._spot_amount_autocomplete(as_interaction(it), ""))
    other = asyncio.run(cog._buy_amount_autocomplete(as_interaction(it), ""))
    assert max(c.value for c in spot) == 1_000_000 and max(c.value for c in other) == 3_000_000


def test_bond_below_minimum_is_refused(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    it = It()
    asyncio.run(invoke(MarketCog.order_bond, cog, it, choice("normal"), 500))
    assert "買えません" in it.sent[-1]["embed"].title and eco.for_guild(G).wallet.balance(U) == INITIAL_BALANCE


def test_positions_and_settle_command_close_each_kind(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    a = eco.for_guild(G)
    a.wallet.credit(U, 20_000, "テスト")
    a.crypto.crypto_buy(U, 1_000, cog._market_svc.latest_price("cs"), "cs")
    a.margin.short_open(U, 2_000, cog._market_svc.latest_price("teio"), 1, "teio")  # 確認ボタンが出ない小さい額
    a.bonds.bond_buy(U, "normal", 1_000, 1.0)
    a.bonds.bond_buy(U, "junk", 1_000, 1.0)  # ジャンク債は売れない: 候補に出ない
    codes = [c for c, _ in cog.positions(U, G)]
    bond_id = next(c for c in codes if c.startswith("bond:"))
    assert codes[:2] == ["spot:cs", "short:teio"] and len(codes) == 3
    for code in ("spot:cs", "short:teio", bond_id):
        it = It()
        asyncio.run(invoke(MarketCog.settle, cog, it, code, None))
        assert "❌" not in it.sent[-1]["embed"].title, code
    assert a.crypto.holdings(U) == [] and a.margin.short_positions(U) == []
    assert [r["kind"] for r in a.bonds.bond_list(U)["active"]] == ["junk"]
    it = It()
    asyncio.run(invoke(MarketCog.settle, cog, it, "bond:99999", None))  # 無い番号
    assert "売れません" in it.sent[-1]["embed"].title
    it = It()
    asyncio.run(invoke(MarketCog.settle, cog, it, "なにか", None))
    assert "決済できません" in it.sent[-1]["embed"].title


def test_settle_half_of_a_spot_position(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    a = eco.for_guild(G)
    a.crypto.crypto_buy(U, 2_000, cog._market_svc.latest_price("cs"), "cs")
    have = a.crypto.crypto_holding(U, "cs")[0]
    asyncio.run(invoke(MarketCog.settle, cog, It(), "spot:cs", 50))
    assert a.crypto.crypto_holding(U, "cs")[0] == have - have // 2


class PanelIt:
    """保有パネルの部品を押す、疑似インタラクション（決済の応答と、パネルの描き直しを記録する）。"""

    def __init__(self, values=None):
        self.user = SimpleNamespace(id=U, display_name="テスト")
        self.guild = SimpleNamespace(id=G)
        self.guild_id = G
        self.data = {"values": values or []}
        self.sent, self.edits, self.modals, self.origin_edits = [], [], [], []
        self.response = SimpleNamespace(
            send_message=self._send, edit_message=self._edit, send_modal=self._modal, defer=self._defer
        )
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, *a, **kw):
        self.sent.append(kw)

    async def _edit(self, **kw):
        self.edits.append(kw)

    async def _modal(self, modal):
        self.modals.append(modal)

    async def _defer(self, **kw):
        return None

    async def edit_original_response(self, **kw):
        self.origin_edits.append(kw)


def open_panel(cog):
    origin = PanelIt()
    asyncio.run(invoke(MarketCog.holdings, cog, origin))
    return origin, origin.sent[-1]["view"]


def press(view, label, it=None):
    it = it or PanelIt()
    asyncio.run(next(b for b in buttons(view) if b.label == label).callback(as_interaction(it)))
    return it


def pick(view, code):
    it = PanelIt([code])
    asyncio.run(selects(view)[0].callback(as_interaction(it)))
    return it


def test_holdings_panel_lists_every_position_in_a_select_and_settles_by_share(eco):
    cog = make_cog(eco)
    a = eco.for_guild(G)
    a.crypto.crypto_buy(U, 2_000, cog._market_svc.latest_price("gold"), "gold")
    a.bonds.bond_buy(U, "normal", 1_000, time.time())
    origin, view = open_panel(cog)
    text = origin.sent[-1]["embed"].description
    assert "GOLD地金" in text and "債券" in text and "通常債" in text
    bond = f"bond:{a.bonds.bond_list(U)['active'][0]['id']}"
    assert [o.value for o in selects(view)[0].options] == ["spot:gold", bond]
    assert all(b.disabled for b in buttons(view) if b.label in ("全部決済", "50%", "25%", "カスタム%"))
    pick(view, "spot:gold")
    assert not any(b.disabled for b in buttons(view) if b.label in ("全部決済", "50%", "25%", "カスタム%"))
    have = a.crypto.crypto_holding(U, "gold")[0]
    it = press(view, "50%")
    assert "売りました" in it.sent[-1]["embed"].title
    assert a.crypto.crypto_holding(U, "gold")[0] == have - have // 2
    assert origin.origin_edits, "決済のあと、元のパネルを描き直す"
    pick(view, bond)  # 債券は、全部だけ
    assert [b.label for b in buttons(view) if not b.disabled][:1] == ["全部決済"]
    assert all(b.disabled for b in buttons(view) if b.label in ("50%", "25%", "カスタム%"))
    press(view, "全部決済")
    assert a.bonds.bond_list(U)["active"] == []
    assert [o.value for o in selects(view)[0].options] == ["spot:gold"]  # 決済した債券は消える


def test_holdings_panel_custom_percent_and_refresh(eco):
    from cogs.market.views import PercentModal

    cog = make_cog(eco)
    a = eco.for_guild(G)
    a.crypto.crypto_buy(U, 2_000, cog._market_svc.latest_price("cs"), "cs")
    have = a.crypto.crypto_holding(U, "cs")[0]
    _origin, view = open_panel(cog)
    pick(view, "spot:cs")
    modal = press(view, "カスタム%").modals[-1]
    assert isinstance(modal, PercentModal)
    modal.percent._value = "abc"
    it = PanelIt()
    asyncio.run(modal.on_submit(as_interaction(it)))
    assert "割合が正しくありません" in it.sent[-1]["embed"].title and a.crypto.crypto_holding(U, "cs")[0] == have
    modal.percent._value = "20"
    asyncio.run(modal.on_submit(as_interaction(PanelIt())))
    assert a.crypto.crypto_holding(U, "cs")[0] == have - have * 20 // 100
    it = press(view, "🔄 更新")
    assert "CSトークン" in it.edits[-1]["embed"].description and it.edits[-1]["view"] is view


def test_holdings_panel_pages_beyond_25_positions(eco, monkeypatch):
    cog = make_cog(eco)
    fake = [(f"bond:{i}", f"🏦 通常債 #{i} 売却") for i in range(1, 31)]
    monkeypatch.setattr(cog, "positions", lambda uid, gid: fake)
    _origin, view = open_panel(cog)
    assert len(selects(view)[0].options) == 25
    press(view, "次 ▶")
    assert [o.value for o in selects(view)[0].options] == [f"bond:{i}" for i in range(26, 31)]
    assert next(b for b in buttons(view) if b.label == "次 ▶").disabled


def test_market_and_holdings_panels_link_to_each_other(eco):
    cog = make_cog(eco)
    _origin, view = open_panel(cog)
    it = press(view, "📈 市況を見る")
    assert "全銘柄" in it.sent[-1]["embed"].title and it.sent[-1]["ephemeral"]
    market_view = it.sent[-1]["view"]
    it = press(market_view, "💼 保有を見る")
    assert "証券" in it.sent[-1]["embed"].title and it.sent[-1]["ephemeral"] and it.sent[-1]["view"]


def test_holdings_view_only_works_for_the_owner(eco):
    from cogs.market.cog import HoldingsView, MarketView

    cog = make_cog(eco)
    sent = []

    class Resp:
        async def send_message(self, *a, **kw):
            sent.append((a, kw))

    stranger = SimpleNamespace(user=SimpleNamespace(id=999), response=Resp())
    for view in (HoldingsView(cog, U, G), MarketView(cog, U, G)):
        assert asyncio.run(view.interaction_check(as_interaction(stranger))) is False
        assert (
            asyncio.run(
                view.interaction_check(as_interaction(SimpleNamespace(user=SimpleNamespace(id=U), response=Resp())))
            )
            is True
        )
    assert len(sent) == 2


def test_empty_holdings_has_no_buttons(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    it = It()
    asyncio.run(invoke(MarketCog.holdings, cog, it))
    assert "まだ持っていません" in it.sent[-1]["embed"].description
    labels = [b.label for b in buttons(it.sent[-1]["view"])]  # 持ち物がなくても、更新と市況への導線は出す
    assert labels == ["🔄 更新", "📈 市況を見る"]


def test_board_without_symbol_shows_summary_with_switcher_and_with_symbol_posts_a_live_chart(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    it = It()
    asyncio.run(invoke(MarketCog.board, cog, it, None, None))
    assert "全銘柄" in it.sent[-1]["embed"].title and it.sent[-1]["ephemeral"] and it.sent[-1]["view"]
    view = it.sent[-1]["view"]
    assert [len(s.options) for s in selects(view)] == [5, len(market.PERIODS)]
    swap = []

    class Resp:
        async def edit_message(self, **kw):
            swap.append(kw["embed"].title)

    view.symbol_select._values = ["teio"]
    asyncio.run(view._on_symbol(SimpleNamespace(response=Resp())))
    view.period_select._values = ["1h"] if "1h" in market.PERIODS else [next(iter(market.PERIODS))]
    asyncio.run(view._on_period(SimpleNamespace(response=Resp())))
    view.symbol_select._values = ["all"]
    asyncio.run(view._on_symbol(SimpleNamespace(response=Resp())))
    assert "TEIO重工" in swap[0] and "TEIO重工" in swap[1] and "全銘柄" in swap[2]
    it = It()
    asyncio.run(invoke(MarketCog.board, cog, it, choice("numa"), None))
    assert "NUMAバイオ" in it.sent[-1]["embed"].title and "view" not in it.sent[-1]


def test_settle_by_percent_sells_that_share_of_a_spot_position(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    a = eco.for_guild(G)
    assert a.crypto.crypto_buy(U, 2_000, cog._market_svc.latest_price("cs"), "cs").ok
    have = a.crypto.crypto_holding(U, "cs")[0]
    assert have > 0
    asyncio.run(invoke(MarketCog.settle, cog, It(), "spot:cs", 25))
    assert a.crypto.crypto_holding(U, "cs")[0] == have - have * 25 // 100
    left = a.crypto.crypto_holding(U, "cs")[0]
    asyncio.run(invoke(MarketCog.settle, cog, It(), "spot:cs", 100))  # 100%は、全部
    assert a.crypto.crypto_holding(U, "cs")[0] == 0 and left > 0


def test_percent_units_are_at_least_one_unit(eco):
    assert orders.units_from(1_000, 1) == 10
    assert orders.units_from(50, 1) == 1  # 端数でも、最低1口
    assert orders.units_from(1_000, 50) == 500
    assert orders.units_from(1_000, 100) is None
    assert orders.units_from(1_000, None) is None


def test_settle_refuses_a_share_of_a_bond_instead_of_selling_all(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    a = eco.for_guild(G)
    a.bonds.bond_buy(U, "normal", 1_000, time.time())
    code = f"bond:{a.bonds.bond_list(U)['active'][0]['id']}"
    it = It()
    asyncio.run(invoke(MarketCog.settle, cog, it, code, 30))
    assert "全部だけ" in it.sent[-1]["embed"].title and len(a.bonds.bond_list(U)["active"]) == 1
    asyncio.run(invoke(MarketCog.settle, cog, It(), code, 100))
    assert a.bonds.bond_list(U)["active"] == []


def test_percent_closes_that_share_of_a_margin_position(eco):
    from tests.domain.test_market import It

    cog = make_cog(eco)
    a = eco.for_guild(G)
    a.wallet.credit(U, 20_000, "テスト")
    assert a.margin.short_open(U, 10_000, cog._market_svc.latest_price("cs"), 1, "cs").ok
    have = a.margin.short_position(U, "cs")[0]
    asyncio.run(invoke(MarketCog.settle, cog, It(), "short:cs", 30))
    assert a.margin.short_position(U, "cs")[0] == have - have * 30 // 100
