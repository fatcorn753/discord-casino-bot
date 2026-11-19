"""帝愛共済（#34）: 口の配布・超過損失の負担・配当・回収・競売・譲渡・動議。"""

import time

import pytest

from domain import kyosai
from infra.db.repositories import open_repositories

G, A, B, C = 10, 1, 2, 3
NOW = time.time()
DAY = 86400
pytestmark = pytest.mark.real_scope


@pytest.fixture
def eco(tmp_path):
    return open_repositories(str(tmp_path / "t.db"), strict=True).for_guild(G)


def units(eco, uid):
    return eco.kyosai.units_of_for(G, uid)


def bank_row(eco, col):
    return eco.db.conn.execute(f"SELECT {col} FROM teiai_bank WHERE guild_id=?", (G,)).fetchone()[0]


def make_house_units(eco, uid):
    """uid の口を帝愛の持ち口にする（破産・休眠と同じ回収の道）。"""
    eco.wallet.balance(uid)
    with eco.db.tx():
        return eco.kyosai.recover_units_for(G, uid, NOW)


# ----------------------------- 口の配布 ----------------------------- #
def test_every_new_account_gets_one_unit(eco):
    eco.wallet.balance(A)
    eco.wallet.balance(B)
    assert units(eco, A) == 1 and units(eco, B) == 1 and units(eco, kyosai.HOUSE) == 0


def test_migration_gives_one_unit_to_existing_accounts(tmp_path):
    path = str(tmp_path / "t.db")
    repos = open_repositories(path, strict=True)
    repos.for_guild(G).wallet.balance(A)
    with repos.db.tx():  # 共済ができる前からいた人（口の表に行がない）
        repos.db.conn.execute("DELETE FROM kyosai_units")
    repos.close()
    again = open_repositories(path, strict=True)
    try:
        assert again.kyosai.units_of_for(G, A) == 1
    finally:
        again.close()


# ----------------------------- 超過損失の負担 ----------------------------- #
def test_excess_loss_is_charged_by_units_and_shortfall_becomes_debt(eco):
    eco.wallet.balance(A)
    eco.wallet.credit(B, 10_000, "テスト")
    cash_b = eco.wallet.balance(B)
    with eco.db.tx():
        eco.kyosai.bank_sync(G, NOW)  # 本番では bank_pay_for の中から呼ばれ、銀行の行はもうある
        eco.db.conn.execute("UPDATE users SET balance=100 WHERE guild_id=? AND user_id=?", (G, A))
        charged = eco.kyosai.charge_excess_for(G, 10_000, NOW)
    assert charged == 10_000
    assert eco.wallet.balance(A) == 0 and eco.debt.debt(A) == 4_900  # 手元 100 + 借金 4,900
    assert eco.wallet.balance(B) == cash_b - 5_000 and eco.debt.debt(B) == 0
    assert bank_row(eco, "kyosai_charged") == 10_000


def test_house_share_of_the_excess_stays_unpaid(eco):
    eco.wallet.balance(A)
    make_house_units(eco, B)  # A 1口・帝愛 1口
    with eco.db.tx():
        paid = eco.bank.bank_pay_for(G, 2_000_000, NOW)  # 預金者なし: 手元も信託もない
    assert eco.debt.debt(A) > 0 and eco.debt.debt(B) == 0  # 口のない B は負担しない
    assert bank_row(eco, "excess_unpaid") > 0 and paid < 2_000_000  # 帝愛の持ち口の分は払われない


# ----------------------------- 配当と休眠 ----------------------------- #
def test_dividend_is_paid_once_a_day_by_units(eco):
    eco.wallet.balance(A)
    eco.wallet.balance(B)
    with eco.db.tx():
        eco.bank.bank_income_for(G, 2_000_000, NOW)  # 負債なし・余剰がたっぷり
    assert eco.kyosai.kyosai_daily(NOW) is None  # 初回は時計を合わせるだけ
    a0, b0 = eco.wallet.balance(A), eco.wallet.balance(B)
    assert eco.kyosai.kyosai_daily(NOW + DAY / 2) is None  # 1日たっていない
    res = eco.kyosai.kyosai_daily(NOW + DAY)
    assert res is not None and res["pool"] == kyosai.DIVIDEND_CAP and res["paid"] == kyosai.DIVIDEND_CAP
    assert eco.wallet.balance(A) - a0 == kyosai.DIVIDEND_CAP // 2 == eco.wallet.balance(B) - b0
    assert bank_row(eco, "kyosai_paid") == kyosai.DIVIDEND_CAP


def test_dividend_pool_comes_only_from_cash_above_the_target_reserve():
    assert kyosai.dividend_pool(29_000, 100_000) == 0  # 準備率 29% < 目標 30%
    assert kyosai.dividend_pool(30_000, 100_000) == 0
    assert kyosai.dividend_pool(50_000, 100_000) == int(20_000 * kyosai.DIVIDEND_SHARE)
    assert kyosai.dividend_pool(10**9, 0) == kyosai.DIVIDEND_CAP


def test_no_dividend_when_the_bank_has_no_cash(eco):
    eco.wallet.credit(A, 300_000, "テスト")
    eco.bank.bank_deposit(A, 200_000, NOW)
    with eco.db.tx():  # 手元を使い切る
        eco.bank.bank_pay_for(G, 40_000, NOW, thermo=False)
    eco.kyosai.kyosai_daily(NOW)
    with eco.db.tx():
        available, liabilities, _ = eco.bank.bank_reserve_for(G, NOW + DAY)
    res = eco.kyosai.kyosai_daily(NOW + DAY)
    assert res is not None and res["pool"] == kyosai.dividend_pool(available, liabilities)


def test_dormant_accounts_lose_their_units_to_the_house(eco):
    eco.wallet.balance(A)
    eco.wallet.balance(B)
    old = NOW - (kyosai.DORMANT_DAYS + 1) * DAY
    with eco.db.tx():  # A は 31日前から動いていない
        eco.db.conn.execute("UPDATE ledger SET ts=? WHERE guild_id=? AND user_id=?", (old, G, A))
        eco.db.conn.execute("UPDATE users SET created_at=? WHERE guild_id=? AND user_id=?", (old, G, A))
    eco.kyosai.kyosai_daily(NOW)
    res = eco.kyosai.kyosai_daily(NOW + DAY)
    assert res is not None and res["recovered"] == 1
    assert units(eco, A) == 0 and units(eco, B) == 1 and units(eco, kyosai.HOUSE) == 1


def test_bankruptcy_recovers_units(eco):
    eco.wallet.balance(B)
    eco.wallet.balance(A)
    with eco.db.tx():
        eco.debt.owe_for(G, A, 80_000, "テスト")
    ok, _ = eco.debt.bankrupt(A)
    assert ok and units(eco, A) == 0 and units(eco, kyosai.HOUSE) == 1


# ----------------------------- 帝愛の持ち口を買う ----------------------------- #
def test_house_units_are_auctioned_with_a_daily_price_drop(eco):
    make_house_units(eco, A)
    assert eco.kyosai.house_price_for(G, NOW) == kyosai.AUCTION_START
    assert eco.kyosai.house_price_for(G, NOW + 3 * DAY) == int(kyosai.AUCTION_START * (1 - 3 * kyosai.AUCTION_DECAY))
    assert eco.kyosai.house_price_for(G, NOW + 100 * DAY) == kyosai.AUCTION_FLOOR
    eco.wallet.credit(B, 20_000, "テスト")
    before = eco.wallet.balance(B)
    ok, cost = eco.kyosai.kyosai_buy_house(B, 1, NOW + 3 * DAY)
    assert ok and cost == 7_000 and eco.wallet.balance(B) == before - 7_000
    assert units(eco, B) == 2 and units(eco, kyosai.HOUSE) == 0
    ok, why = eco.kyosai.kyosai_buy_house(B, 1, NOW + 3 * DAY)
    assert not ok and why.code == "kyosai_house_short"


# ----------------------------- 譲渡 ----------------------------- #
def test_offer_and_accept_moves_units_and_money(eco):
    eco.wallet.balance(A)
    eco.wallet.credit(B, 1_000, "テスト")
    a0, b0 = eco.wallet.balance(A), eco.wallet.balance(B)
    ok, offer_id = eco.kyosai.kyosai_offer(A, B, 1, 500, NOW)
    assert ok
    ok, why = eco.kyosai.kyosai_offer(A, C, 1, 500, NOW)  # 申し出中の口は、もう出せない
    assert not ok and why.code == "kyosai_units_short"
    assert eco.kyosai.kyosai_offers_of(B, NOW)[0]["id"] == offer_id
    ok, _ = eco.kyosai.kyosai_accept(A, offer_id, NOW)  # 自分あてでない申し出は受けられない
    assert not ok
    ok, row = eco.kyosai.kyosai_accept(B, offer_id, NOW)
    assert ok and row["units"] == 1 and row["price"] == 500
    assert units(eco, A) == 0 and units(eco, B) == 2
    assert eco.wallet.balance(A) == a0 + 500 and eco.wallet.balance(B) == b0 - 500


def test_offer_expires_and_can_be_cancelled(eco):
    eco.wallet.balance(A)
    eco.wallet.balance(B)
    ok, offer_id = eco.kyosai.kyosai_offer(A, B, 1, 0, NOW)
    ok, why = eco.kyosai.kyosai_accept(B, offer_id, NOW + kyosai.OFFER_HOURS * 3600 + 1)
    assert not ok and why.code == "kyosai_offer_not_found"
    ok, offer_id = eco.kyosai.kyosai_offer(A, B, 1, 0, NOW + 2 * DAY)  # 期限切れの申し出の口は、また出せる
    assert ok and eco.kyosai.kyosai_cancel(B, offer_id) and not eco.kyosai.kyosai_cancel(B, offer_id)


def test_trading_is_locked_while_someone_is_in_grace(eco):
    eco.wallet.balance(A)
    eco.wallet.balance(B)
    eco.wallet.balance(C)
    with eco.db.tx():
        eco.db.conn.execute(
            "UPDATE users SET debt=100000, grace_until=? WHERE guild_id=? AND user_id=?", (NOW + DAY, G, C)
        )
    ok, why = eco.kyosai.kyosai_offer(A, B, 1, 0, NOW)
    assert not ok and why.code == "kyosai_gate_locked"


def test_trading_is_locked_when_the_reserve_is_low(eco):
    eco.wallet.credit(A, 300_000, "テスト")
    eco.wallet.balance(B)
    eco.bank.bank_deposit(A, 200_000, NOW)  # θ = 0.20
    with eco.db.tx():
        eco.bank.bank_pay_for(G, 10_000, NOW)  # 手元が減って θ < 0.20
    ok, why = eco.kyosai.kyosai_offer(A, B, 1, 0, NOW)
    assert not ok and why.code == "kyosai_gate_locked"


# ----------------------------- 動議 ----------------------------- #
def test_motion_passes_by_units_and_changes_the_trust_premium(eco):
    for uid in (A, B, C):
        eco.wallet.balance(uid)
    ok, motion_id = eco.kyosai.kyosai_propose(A, 0.003, NOW)  # 提案者は自動で賛成
    assert ok
    ok, why = eco.kyosai.kyosai_propose(B, 0.001, NOW)
    assert not ok and why.code == "kyosai_motion_open"
    eco.kyosai.kyosai_vote(B, False, NOW)
    eco.kyosai.kyosai_vote(C, True, NOW)
    assert eco.kyosai.kyosai_close_motion(NOW + 60) is None  # 締切前
    res = eco.kyosai.kyosai_close_motion(NOW + kyosai.MOTION_HOURS * 3600 + 1)
    assert res is not None and res["id"] == motion_id and res["status"] == "passed"
    assert (res["yes"], res["no"]) == (2, 1)
    assert eco.bank.trust_premium_for(G) == pytest.approx(0.003)


def test_tied_motion_is_rejected_and_premium_stays(eco):
    eco.wallet.balance(A)
    eco.wallet.balance(B)
    before = eco.bank.trust_premium_for(G)
    eco.kyosai.kyosai_propose(A, 0.004, NOW)
    eco.kyosai.kyosai_vote(B, False, NOW)
    res = eco.kyosai.kyosai_close_motion(NOW + kyosai.MOTION_HOURS * 3600 + 1)
    assert res is not None and res["status"] == "rejected"
    assert eco.bank.trust_premium_for(G) == before


def test_premium_out_of_range_is_refused(eco):
    eco.wallet.balance(A)
    ok, why = eco.kyosai.kyosai_propose(A, kyosai.PREMIUM_MAX + 0.001, NOW)
    assert not ok and why.code == "kyosai_bad_premium"


# ----------------------------- 画面 ----------------------------- #
def test_status_embed_shows_units_motion_and_offers(eco):
    from cogs.kyosai.cog import status_embed

    eco.wallet.balance(A)
    eco.wallet.balance(B)
    eco.kyosai.kyosai_propose(A, 0.002, NOW)
    eco.kyosai.kyosai_offer(A, B, 1, 300, NOW)
    s = eco.kyosai.kyosai_status(A, NOW)
    assert s["units"] == 1 and s["total"] == 2 and s["holders"] == 2
    embed = status_embed(s, eco.kyosai.kyosai_offers_of(A, NOW), A)
    text = embed.description or ""
    assert "1口" in text and "動議 #" in text and "申し出 #" in text
