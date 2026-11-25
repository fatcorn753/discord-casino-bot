"""帝愛共済（#34・#93・#94）: 総口数・超過損失の負担・配当・回収・競り・譲渡・動議。"""

import time

import pytest

from domain import kyosai, p2p, shop, teiai
from infra.db.repositories import open_repositories
from tests.support import give_test_kyosai_units, set_test_balance, set_test_debt, set_test_grace

G, A, B, C = 10, 1, 2, 3
NOW = time.time()
DAY = 86400
TOTAL = kyosai.TOTAL_UNITS
pytestmark = pytest.mark.real_scope


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"), strict=True).for_guild(G)


def units(eco, uid):
    return eco.kyosai.units_of_for(G, uid)


def bank_row(eco, col):
    mapping = {
        "kyosai_paid": ("kyosai_state", "paid"),
        "kyosai_charged": ("kyosai_state", "charged"),
        "kyosai_div_ts": ("kyosai_state", "div_ts"),
        "thermo_load": ("teiai_thermostat", "load"),
        "excess_unpaid": ("teiai_trust", "excess_unpaid"),
    }
    table, target = mapping.get(col, ("teiai_bank", col))
    row = eco.db.conn.execute(f"SELECT {target} FROM {table} WHERE guild_id=?", (G,)).fetchone()
    return row[0] if row is not None else 0.0


def give(eco, uid, n):
    """帝愛の持ち口から uid へ n 口移す（競りで落札したのと同じ状態）。"""
    eco.wallet.balance(uid)
    with eco.db.tx():
        give_test_kyosai_units(eco, uid, n, guild_id=G, now=NOW)


def total_units(eco):
    users_sum = eco.db.conn.execute("SELECT COALESCE(SUM(units), 0) FROM kyosai_units WHERE guild_id=?", (G,)).fetchone()[0]
    house_row = eco.db.conn.execute("SELECT house_units FROM kyosai_state WHERE guild_id=?", (G,)).fetchone()
    return users_sum + (house_row[0] if house_row is not None else 0)


# ----------------------------- 総口数（#93） ----------------------------- #
def test_new_accounts_get_no_units_and_the_house_holds_the_fixed_total(eco):
    eco.wallet.balance(A)
    with eco.db.tx():
        eco.kyosai.bank_sync(G, NOW)
    assert units(eco, A) == 0 and units(eco, kyosai.HOUSE) == TOTAL == total_units(eco)


def test_migration_rescales_old_holdings_to_the_fixed_total_keeping_the_ratio(tmp_path):
    path = str(tmp_path / "t.db")
    repos = open_repositories(path, strict=True)
    with repos.db.tx():  # 1人1口ずつ配っていた頃: A 1口・B 1口・帝愛 1口
        for uid in (A, B):
            repos.db.conn.execute("INSERT INTO kyosai_units (guild_id, user_id, units) VALUES (?, ?, 1)", (G, uid))
        repos.db.conn.execute(
            "INSERT INTO kyosai_state (guild_id, house_units) VALUES (?, 1) "
            "ON CONFLICT(guild_id) DO UPDATE SET house_units=1",
            (G,),
        )
        repos.db.conn.execute("PRAGMA user_version = 0")
    repos.close()
    for _ in range(2):  # 何度開いても同じ
        again = open_repositories(path, strict=True)
        try:
            got = [again.kyosai.units_of_for(G, u) for u in (A, B, kyosai.HOUSE)]
        finally:
            again.close()
        assert sum(got) == TOTAL and max(got) - min(got) <= 1


# ----------------------------- 超過損失の負担 ----------------------------- #
def test_excess_loss_is_charged_by_units_and_shortfall_becomes_debt_with_a_ledger_entry(eco):
    give(eco, A, TOTAL // 2)
    give(eco, B, TOTAL // 2)
    eco.wallet.credit(B, 10_000, "テスト")
    cash_b = eco.wallet.balance(B)
    with eco.db.tx():
        set_test_balance(eco, A, 100, guild_id=G)
        charged = eco.kyosai.charge_excess_for(G, 10_000, NOW)
    assert charged == 10_000
    assert eco.wallet.balance(A) == 0 and eco.debt.debt(A) == 4_900  # 手元 100 + 借金 4,900
    assert eco.wallet.balance(B) == cash_b - 5_000 and eco.debt.debt(B) == 0
    assert bank_row(eco, "kyosai_charged") == 10_000
    reasons = [r[0] for r in eco.db.conn.execute("SELECT reason FROM ledger WHERE guild_id=? AND user_id=?", (G, A))]
    assert "共済:超過損失の負担（借金 4,900ペリカ）" in reasons  # 借金になった分も履歴に残る


def test_house_share_of_the_excess_stays_unpaid(eco):
    give(eco, A, TOTAL // 2)  # 残り半分は帝愛の持ち口
    with eco.db.tx():
        paid = eco.bank.bank_pay_for(G, 2_000_000, NOW)  # 預金者なし: 手元も信託もない
    assert eco.debt.debt(A) > 0
    assert bank_row(eco, "excess_unpaid") > 0 and paid < 2_000_000  # 帝愛の持ち口の分は払われない


# ----------------------------- 配当と休眠 ----------------------------- #
def test_dividend_is_paid_once_a_day_by_units_without_going_through_the_thermostat(eco):
    give(eco, A, TOTAL // 2)
    give(eco, B, TOTAL // 2)
    with eco.db.tx():
        eco.bank.bank_income_for(G, 10_000, NOW)
        eco.bank.bank_set_for(G, 2_000_000, 0)  # 負債なし・余剰がたっぷり（サーモスタットの熱量も残した状態で試す）
    assert eco.kyosai.kyosai_daily(NOW) is None  # 初回は時計を合わせるだけ
    a0, b0 = eco.wallet.balance(A), eco.wallet.balance(B)
    load0 = bank_row(eco, "thermo_load")
    assert eco.kyosai.kyosai_daily(NOW + DAY / 2) is None  # 1日たっていない
    res = eco.kyosai.kyosai_daily(NOW + DAY)
    assert res is not None and res["pool"] == kyosai.DIVIDEND_CAP and res["paid"] == kyosai.DIVIDEND_CAP
    assert eco.wallet.balance(A) - a0 == kyosai.DIVIDEND_CAP // 2 == eco.wallet.balance(B) - b0
    assert bank_row(eco, "kyosai_paid") == kyosai.DIVIDEND_CAP
    assert bank_row(eco, "thermo_load") == pytest.approx(load0)  # 配当は余剰の分配で、損失ではない


def test_dividend_pool_comes_only_from_cash_above_the_target_reserve():
    assert kyosai.dividend_pool(29_000, 100_000) == 0  # 準備率 29% < 目標 30%
    assert kyosai.dividend_pool(30_000, 100_000) == 0
    assert kyosai.dividend_pool(50_000, 100_000) == int(20_000 * kyosai.DIVIDEND_SHARE)
    assert kyosai.dividend_pool(10**9, 0) == kyosai.DIVIDEND_CAP


def _age_ledger(eco, uid, ts):
    with eco.db.tx():
        eco.db.conn.execute("UPDATE ledger SET ts=? WHERE guild_id=? AND user_id=?", (ts, G, uid))
        eco.db.conn.execute("UPDATE accounts SET created_at=? WHERE guild_id=? AND user_id=?", (ts, G, uid))


def test_dormant_accounts_lose_their_units_to_the_house(eco):
    give(eco, A, 100)
    give(eco, B, 100)
    old = NOW - (kyosai.DORMANT_DAYS + 1) * DAY
    _age_ledger(eco, A, old)  # A は 31日前から動いていない
    with eco.db.tx():  # 本人が何もしなくても起きる動きは、動いたことに数えない
        for reason, kind in (
            ("共済:配当", "kyosai_dividend"),
            ("共済:超過損失の負担（借金 10ペリカ）", "kyosai_excess_charge"),
            ("帝愛信託:利息", "trust_interest"),
        ):
            eco.db.conn.execute(
                "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts, kind) "
                "VALUES (?, ?, 0, 0, ?, ?, ?)",
                (G, A, reason, NOW, kind),
            )
    eco.kyosai.kyosai_daily(NOW)
    res = eco.kyosai.kyosai_daily(NOW + DAY)
    assert res is not None and res["recovered"] == 100
    assert units(eco, A) == 0 and units(eco, B) == 100


def test_trading_units_counts_as_activity(eco):
    give(eco, A, 100)
    old = NOW - (kyosai.DORMANT_DAYS + 1) * DAY
    _age_ledger(eco, A, old)
    with eco.db.tx():  # 口の売買は、本人の操作
        eco.db.conn.execute(
            "INSERT INTO ledger (guild_id, user_id, delta, balance_after, reason, ts) VALUES (?, ?, 0, 0, ?, ?)",
            (G, A, "共済:口の売却", NOW),
        )
    eco.kyosai.kyosai_daily(NOW)
    eco.kyosai.kyosai_daily(NOW + DAY)
    assert units(eco, A) == 100


def test_bankruptcy_recovers_units(eco):
    give(eco, A, 100)
    house = units(eco, kyosai.HOUSE)
    with eco.db.tx():
        eco.debt.owe_for(G, A, 80_000, "テスト")
    ok, _ = eco.debt.bankrupt(A)
    assert ok and units(eco, A) == 0 and units(eco, kyosai.HOUSE) == house + 100


# ----------------------------- 帝愛の競り（#93） ----------------------------- #
def test_house_units_are_sold_by_ascending_auction_and_the_price_goes_to_the_bank(eco):
    events = eco.kyosai.kyosai_auction_tick(NOW)
    assert [e["event"] for e in events] == ["listed"] and events[0]["units"] == kyosai.AUCTION_LOT
    eco.wallet.credit(A, 50_000, "テスト")
    eco.wallet.credit(B, 50_000, "テスト")
    a0, b0 = eco.wallet.balance(A), eco.wallet.balance(B)
    ok, why = eco.kyosai.kyosai_bid(A, 0, NOW)
    assert not ok and why.code == "kyosai_bid_low"
    ok, _ = eco.kyosai.kyosai_bid(A, 1_000, NOW)
    assert ok and eco.wallet.balance(A) == a0 - 1_000  # 入札額は預かる
    ok, why = eco.kyosai.kyosai_bid(B, 1_001, NOW)
    assert not ok and why.code == "kyosai_bid_low"  # 5%（最低100）以上高く
    ok, row = eco.kyosai.kyosai_bid(B, 1_100, NOW)
    assert ok and eco.wallet.balance(A) == a0  # 上回られたら返す
    cash0 = eco.bank.bank_status(NOW)["cash"]
    assert eco.kyosai.kyosai_auction_tick(NOW + 60) == []  # 締切前
    events = eco.kyosai.kyosai_auction_tick(row["ends"] + 1)
    assert [e["event"] for e in events] == ["sold", "listed"]  # 落札のあと、次の口を出す
    assert units(eco, B) == kyosai.AUCTION_LOT and eco.wallet.balance(B) == b0 - 1_100
    s = eco.bank.bank_status(row["ends"] + 1)
    assert s["cash"] == pytest.approx(cash0 + 1_100 - s["thermostat"]["burned"])
    assert total_units(eco) == TOTAL


def test_auction_refuses_debtors_and_relists_quietly_when_unsold(eco):
    eco.kyosai.kyosai_auction_tick(NOW)
    eco.wallet.balance(A)
    with eco.db.tx():
        set_test_debt(eco, A, 100, guild_id=G)
    ok, why = eco.kyosai.kyosai_bid(A, 10, NOW)
    assert not ok and why.code == "debt_blocks"
    events = eco.kyosai.kyosai_auction_tick(NOW + kyosai.AUCTION_HOURS * 3600 + 1)
    assert [e["event"] for e in events] == ["unsold"]  # 出し直しは告知しない
    assert eco.kyosai.kyosai_status(A, NOW + kyosai.AUCTION_HOURS * 3600 + 2)["auction"] is not None


def test_no_auction_is_shown_when_the_house_has_no_units(eco):
    give(eco, A, TOTAL)
    assert eco.kyosai.kyosai_auction_tick(NOW) == []
    s = eco.kyosai.kyosai_status(A, NOW)
    assert s["house"] == 0 and s["auction"] is None


def test_auction_keeps_running_while_transfers_are_gated(eco):
    eco.kyosai.kyosai_auction_tick(NOW)
    eco.wallet.credit(A, 10_000, "テスト")
    eco.wallet.balance(C)
    with eco.db.tx():
        set_test_debt(eco, C, 100_000, guild_id=G)
        set_test_grace(eco, C, NOW + DAY, guild_id=G)
    ok, _ = eco.kyosai.kyosai_bid(A, 500, NOW)
    assert ok


# ----------------------------- 譲渡 ----------------------------- #
def test_offer_and_accept_moves_units_and_money_minus_the_fee(eco):
    give(eco, A, 10)
    eco.wallet.credit(B, 1_000, "テスト")
    a0, b0 = eco.wallet.balance(A), eco.wallet.balance(B)
    ok, offer_id = eco.kyosai.kyosai_offer(A, B, 10, 500, NOW)
    assert ok
    ok, why = eco.kyosai.kyosai_offer(A, C, 1, 500, NOW)  # 申し出中の口は、もう出せない
    assert not ok and why.code == "kyosai_units_short"
    assert eco.kyosai.kyosai_offers_of(B, NOW)[0]["id"] == offer_id
    ok, _ = eco.kyosai.kyosai_accept(A, offer_id, NOW)  # 自分あてでない申し出は受けられない
    assert not ok
    ok, row = eco.kyosai.kyosai_accept(B, offer_id, NOW)
    fee = p2p.fee_of(500)
    assert ok and row["units"] == 10 and row["price"] == 500 and row["fee"] == fee
    assert units(eco, A) == 0 and units(eco, B) == 10
    assert eco.wallet.balance(A) == a0 + 500 - fee and eco.wallet.balance(B) == b0 - 500


def test_transfers_share_the_p2p_daily_pair_limit(eco):
    """高値で譲って 0 で返す送金の抜け道をふさぐ（#34 のレビュー）。"""
    give(eco, A, 10)
    eco.wallet.credit(B, p2p.PAIR_DAILY_LIMIT * 2, "テスト")
    ok, why = eco.kyosai.kyosai_offer(A, B, 1, p2p.PAIR_DAILY_LIMIT + 1, NOW)
    assert not ok and why.code == "p2p_pair_limit"
    ok, first = eco.kyosai.kyosai_offer(A, B, 1, p2p.PAIR_DAILY_LIMIT, NOW)
    assert ok and eco.kyosai.kyosai_accept(B, first, NOW)[0]
    ok, second = eco.kyosai.kyosai_offer(A, B, 1, 1, NOW)
    ok, why = eco.kyosai.kyosai_accept(B, second, NOW)
    assert not ok and why.code == "p2p_pair_limit"
    ok, third = eco.kyosai.kyosai_offer(A, B, 1, 1, NOW + DAY + 1)
    assert eco.kyosai.kyosai_accept(B, third, NOW + DAY + 1)[0]  # 24時間たてば、また


def test_p2p_pair_limit_also_counts_kyosai_transfers(eco):
    """共済譲渡で 24時間上限まで取引したあとは、同じ2人の自由市場の購入も拒否される（双方向に合算）。"""
    give(eco, A, 10)
    eco.wallet.credit(B, p2p.PAIR_DAILY_LIMIT * 2, "テスト")
    ok, offer_id = eco.kyosai.kyosai_offer(A, B, 1, p2p.PAIR_DAILY_LIMIT, NOW)
    assert ok and eco.kyosai.kyosai_accept(B, offer_id, NOW)[0]  # 共済譲渡で 10万
    with eco.db.tx():
        eco.db.conn.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty=qty+1",
            (G, A, shop.CARROT),
        )
    ok, lid = eco.p2p.p2p_list(A, "item", shop.CARROT, 1, "fixed", 1_000, None, NOW)
    assert ok
    ok, why = eco.p2p.p2p_buy(B, lid, NOW)
    assert not ok and why.code == "p2p_pair_limit" and why.params["used"] == p2p.PAIR_DAILY_LIMIT


def test_offer_expires_and_can_be_cancelled(eco):
    give(eco, A, 1)
    eco.wallet.balance(B)
    ok, offer_id = eco.kyosai.kyosai_offer(A, B, 1, 0, NOW)
    ok, why = eco.kyosai.kyosai_accept(B, offer_id, NOW + kyosai.OFFER_HOURS * 3600 + 1)
    assert not ok and why.code == "kyosai_offer_not_found"
    ok, offer_id = eco.kyosai.kyosai_offer(A, B, 1, 0, NOW + 2 * DAY)  # 期限切れの申し出の口は、また出せる
    assert ok and eco.kyosai.kyosai_cancel(B, offer_id) and not eco.kyosai.kyosai_cancel(B, offer_id)


def test_trading_is_locked_while_someone_is_in_grace(eco):
    give(eco, A, 1)
    eco.wallet.balance(B)
    eco.wallet.balance(C)
    with eco.db.tx():
        set_test_debt(eco, C, 100_000, guild_id=G)
        set_test_grace(eco, C, NOW + DAY, guild_id=G)
    ok, why = eco.kyosai.kyosai_offer(A, B, 1, 0, NOW)
    assert not ok and why.code == "kyosai_gate_locked"


def test_trading_is_locked_when_the_reserve_is_low(eco):
    give(eco, A, 1)
    eco.wallet.credit(A, 300_000, "テスト")
    eco.wallet.balance(B)
    eco.bank.bank_deposit(A, 200_000, NOW)  # θ = 0.20
    with eco.db.tx():
        eco.bank.bank_pay_for(G, 10_000, NOW)  # 手元が減って θ < 0.20
    ok, why = eco.kyosai.kyosai_offer(A, B, 1, 0, NOW)
    assert not ok and why.code == "kyosai_gate_locked"


# ----------------------------- 取引停止時の入札の引き戻し ----------------------------- #
def _open_auction_row(eco):
    return eco.db.conn.execute(
        "SELECT id, top_bid, top_bidder FROM kyosai_auctions WHERE guild_id=? AND status='open'", (G,)
    ).fetchone()


def test_grace_enter_cancels_the_open_auction_bid_and_refunds_the_escrow(eco):
    """破産猶予に入ったら、進行中の競りの入札が取り消されて預かり金が手元へ戻る（#25 同型の逃避をふさぐ）。"""
    eco.kyosai.kyosai_auction_tick(NOW)
    eco.wallet.credit(A, 50_000, "テスト")
    a0 = eco.wallet.balance(A)
    ok, _ = eco.kyosai.kyosai_bid(A, 1_000, NOW)
    assert ok and eco.wallet.balance(A) == a0 - 1_000
    with eco.db.tx():
        eco.debt.owe_for(G, A, a0 + 20_000, "テスト")  # 手元を全額差し押さえたうえで借金 → 破産猶予へ
    assert eco.bank.grace_until(A) is not None
    row = _open_auction_row(eco)
    assert row["top_bid"] == 0 and row["top_bidder"] is None
    assert eco.wallet.balance(A) == teiai.SEED + 1_000  # 種銭 + 入札の返金
    reasons = [r[0] for r in eco.db.conn.execute("SELECT reason FROM ledger WHERE guild_id=? AND user_id=?", (G, A))]
    assert "共済:競りの入札の返金（取引停止）" in reasons


def test_self_bankruptcy_pulls_the_bid_back_into_the_estate_and_leaves_no_escape(eco):
    """自己破産でも預かり金が引き戻されて破産財団に組み込まれ、破産後に手元や共済口へ逃がせない。"""
    eco.kyosai.kyosai_auction_tick(NOW)
    eco.wallet.credit(A, 50_000, "テスト")
    a0 = eco.wallet.balance(A)
    ok, row = eco.kyosai.kyosai_bid(A, 1_000, NOW)
    assert ok
    with eco.db.tx():
        set_test_debt(eco, A, 200_000, guild_id=G)
    ok, result = eco.debt.bankrupt(A)
    assert ok and eco.debt.status(A) == teiai.BANKRUPT and eco.debt.debt(A) > 0
    auction = _open_auction_row(eco)
    assert auction["top_bid"] == 0 and auction["top_bidder"] is None
    assert result["seized"] == a0  # 戻った預かり金も現金ごと破産財団へ
    assert eco.wallet.balance(A) == 0 and result["seed"] == 0 and units(eco, A) == 0
    events = eco.kyosai.kyosai_auction_tick(row["ends"] + 1)
    assert [e["event"] for e in events] == ["unsold"]  # 締めても A には口が渡らない（出し直しは告知しない）
    assert units(eco, A) == 0 and _open_auction_row(eco)["top_bidder"] is None


# ----------------------------- 動議（#94） ----------------------------- #
CLOSE = kyosai.MOTION_HOURS * 3600 + 1


def test_motion_needs_more_yes_than_no_and_the_minimum_yes_share(eco):
    give(eco, A, 2_000)
    give(eco, B, 1_000)
    give(eco, C, 7_000)  # 流通 10,000口 → 賛成 2,500口 以上が要る
    ok, m = eco.kyosai.kyosai_propose(A, 0.003, NOW)  # 提案者は自動で賛成
    assert ok
    eco.kyosai.kyosai_vote(B, False, NOW)
    assert eco.kyosai.kyosai_close_motions(NOW + 60) == []  # 締切前
    [res] = eco.kyosai.kyosai_close_motions(NOW + CLOSE)
    assert res["id"] == m["id"] and (res["yes"], res["no"]) == (2_000, 1_000)
    assert res["status"] == "rejected"  # 賛成が多くても、2,000 < 2,500 で否決（棄権が多い）
    ok, m = eco.kyosai.kyosai_propose(C, 0.003, NOW + CLOSE)
    [res] = eco.kyosai.kyosai_close_motions(NOW + 2 * CLOSE)
    assert res["status"] == "passed" and eco.bank.trust_premium_for(G) == pytest.approx(0.003)


def test_voting_no_never_helps_a_motion_pass(eco):
    """最低「賛成率」なので、反対を入れて定足数を満たしてしまう逆転が起きない。"""
    give(eco, A, 2_000)
    give(eco, B, 3_000)
    eco.wallet.balance(C)
    eco.kyosai.kyosai_propose(A, 0.003, NOW)
    eco.kyosai.kyosai_vote(B, False, NOW)
    [res] = eco.kyosai.kyosai_close_motions(NOW + CLOSE)
    assert res["status"] == "rejected"


def test_parallel_motions_apply_in_closing_order_last_write_wins(eco):
    for uid in (A, B, C):
        give(eco, uid, 3_000)
    ok, first = eco.kyosai.kyosai_propose(A, 0.002, NOW)
    ok2, second = eco.kyosai.kyosai_propose(B, 0.004, NOW + 60)
    assert ok and ok2  # いくつも並行できる
    ok, why = eco.kyosai.kyosai_vote(C, True, NOW + 120)
    assert not ok and why.code == "kyosai_motion_ambiguous"  # 2件あるときは番号が要る
    for mid in (first["id"], second["id"]):
        assert eco.kyosai.kyosai_vote(C, True, NOW + 120, mid)[0]
    res = eco.kyosai.kyosai_close_motions(NOW + 60 + CLOSE)
    assert [r["id"] for r in res] == [first["id"], second["id"]] and all(r["status"] == "passed" for r in res)
    assert eco.bank.trust_premium_for(G) == pytest.approx(0.004)  # あとで締めたほうが勝つ


def test_motion_spam_guards(eco, monkeypatch):
    give(eco, A, kyosai.MOTION_MIN_UNITS)
    give(eco, B, kyosai.MOTION_MIN_UNITS - 1)
    ok, why = eco.kyosai.kyosai_propose(B, 0.002, NOW)
    assert not ok and why.code == "kyosai_motion_units"
    assert eco.kyosai.kyosai_propose(A, 0.002, NOW)[0]
    ok, why = eco.kyosai.kyosai_propose(A, 0.003, NOW + 60)
    assert not ok and why.code == "kyosai_motion_open"  # 1人1件まで
    monkeypatch.setattr(kyosai, "MOTION_MAX_OPEN_PER_USER", 2)
    ok, why = eco.kyosai.kyosai_propose(A, 0.003, NOW + 60)
    assert not ok and why.code == "kyosai_motion_cooldown"  # 前に出してから、まだ時間がたっていない
    assert eco.kyosai.kyosai_propose(A, 0.003, NOW + kyosai.MOTION_COOLDOWN_HOURS * 3600 + 1)[0]


def test_premium_must_be_positive_but_has_no_policy_cap(eco):
    give(eco, A, kyosai.MOTION_MIN_UNITS)
    for bad in (0.0, -0.001, kyosai.PREMIUM_LIMIT + 0.001):
        ok, why = eco.kyosai.kyosai_propose(A, bad, NOW)
        assert not ok and why.code == "kyosai_bad_premium"
    assert eco.kyosai.kyosai_propose(A, 0.05, NOW)[0]  # 旧上限 0.5%/日 を超えても出せる


# ----------------------------- 画面 ----------------------------- #
def test_status_embed_shows_units_auction_motions_and_offers(eco):
    from cogs.kyosai.cog import motion_embed, status_embed

    give(eco, A, 150)
    eco.wallet.balance(B)
    eco.kyosai.kyosai_auction_tick(NOW)
    ok, m = eco.kyosai.kyosai_propose(A, 0.002, NOW)
    eco.kyosai.kyosai_offer(A, B, 1, 300, NOW)
    s = eco.kyosai.kyosai_status(A, NOW)
    assert s["units"] == 150 and s["total"] == TOTAL and s["holders"] == 1
    text = status_embed(s, eco.kyosai.kyosai_offers_of(A, NOW), A).description or ""
    assert "150口" in text and "1.50%" in text and "競り" in text and "動議 #" in text and "申し出 #" in text
    motion = eco.kyosai.kyosai_motion(m["id"], NOW)
    assert motion is not None and motion["yes"] == 150
    assert f"#{m['id']}" in (motion_embed(motion).title or "")
