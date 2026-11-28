"""無リスク金利 r_f の動き（#18）。準備率 θ に連動し、債券の利率は買ったときに固定される。"""

import math

import pytest

from domain import bonds, rates, teiai
from domain.types import INITIAL_BALANCE
from infra.db.repositories import open_repositories

NOW = 1_000_000.0
DAY = 86400


class Gauss:
    """乱数を固定する: gauss() が常に value を返す（0 なら揺らぎなし）。"""

    def __init__(self, value=0.0):
        self.value = value

    def gauss(self, _mu, _sigma):
        return self.value


def normal_pct(rf):
    return rates.bond_rate_pct(bonds.NORMAL, rf)


def test_the_target_rises_as_the_reserve_ratio_falls():
    targets = [rates.rf_target(t) for t in (0.0, 0.05, 0.15, 0.225, 0.4, 1.0)]
    assert targets == sorted(targets, reverse=True)
    assert normal_pct(rates.rf_target(0.0)) == pytest.approx(teiai.RF_MAX_PCT)
    assert normal_pct(rates.rf_target(0.225)) == pytest.approx(4.0, abs=0.1)  # 平時は、これまでの 4%
    assert rates.rf_target(None) == rates.BASE_RISK_FREE_PER_DAY
    assert normal_pct(rates.BASE_RISK_FREE_PER_DAY) == bonds.NORMAL.rate


def test_the_step_reverts_to_the_target_and_stays_in_the_band():
    r = rates.BASE_RISK_FREE_PER_DAY
    mu = rates.rf_target(0.0)
    halfway = rates.rf_step(r, 0.0, 0.5, 0.0)
    assert r < halfway < mu
    assert rates.rf_step(r, 0.0, 30, 0.0) == pytest.approx(mu)  # 長く止まっていても、目標を超えない
    assert rates.rf_step(r, 0.0, 1 / 1440, 1e9) == rates.RF_MAX  # 揺らぎが大きくても、上限で止まる
    assert rates.rf_step(r, 0.0, 1 / 1440, -1e9) == rates.RF_MIN
    assert rates.rf_step(r, 0.0, 0, 5.0) == r


def test_the_junk_rate_keeps_its_spread_and_scales_with_its_own_maturity():
    """ジャンク債（1日）には、通常債（3日）の利率の動きをそのまま足さず、満期の長さに比例して効かせる（#18）。"""
    assert rates.bond_rate_pct(bonds.JUNK, rates.BASE_RISK_FREE_PER_DAY) == pytest.approx(bonds.JUNK.rate)
    rf = rates.rf_target(0.0)
    spread = rates.per_day(bonds.JUNK.rate, bonds.JUNK.hours) - rates.BASE_RISK_FREE_PER_DAY
    expected = math.expm1((rf + spread) * bonds.JUNK.hours / 24) * 100
    assert rates.bond_rate_pct(bonds.JUNK, rf) == pytest.approx(round(expected, 2))
    naive = bonds.JUNK.rate + normal_pct(rf) - bonds.NORMAL.rate  # 3日分の動きを1日債に足した、直す前の値
    assert rates.bond_rate_pct(bonds.JUNK, rf) - bonds.JUNK.rate < (naive - bonds.JUNK.rate) / 2


def test_the_sale_discount_equals_the_old_constant_at_the_base_rate():
    assert rates.sale_discount_per_hour(rates.BASE_RISK_FREE_PER_DAY) == pytest.approx(bonds.SALE_DISCOUNT_PER_HOUR)


@pytest.fixture
def eco(tmp_path):
    e = open_repositories(str(tmp_path / "t.db")).for_guild(0)
    e.bank.bank_sync_for(0, NOW)
    e.bank.bank_set_for(0, 1_000_000, 0)
    return e


def set_rf(eco, rf):
    eco.db.conn.execute("UPDATE teiai_trust SET rf=?, rf_ts=? WHERE guild_id=0", (rf, NOW))


def test_the_bank_tick_starts_at_the_base_rate_and_climbs_when_cash_is_short(eco):
    eco.wallet.credit(1, 100_000, "テスト")
    eco.bank.bank_deposit(1, 100_000, NOW)  # 信託 10万、現金はその 20%
    eco.bank.bank_set_for(0, 1_000, 0)  # 準備率 1%（取り付け寸前）
    assert eco.bank.bank_rf_tick(NOW, Gauss()) == rates.BASE_RISK_FREE_PER_DAY
    r = eco.bank.bank_rf_tick(NOW + DAY, Gauss())
    assert r > rates.BASE_RISK_FREE_PER_DAY
    s = eco.bank.bank_status(NOW + DAY)
    assert s["risk_free"] == pytest.approx(r) and s["trust_rate"] == pytest.approx(r + teiai.TRUST_PREMIUM_PER_DAY)
    assert s["rf_target"] > r


def test_the_rate_falls_when_the_bank_is_flush(eco):
    eco.wallet.credit(1, 10_000, "テスト")
    eco.bank.bank_deposit(1, 10_000, NOW)  # 現金 100万に対して信託 1万（準備率 100倍）
    eco.bank.bank_rf_tick(NOW, Gauss())
    assert eco.bank.bank_rf_tick(NOW + DAY, Gauss()) < rates.BASE_RISK_FREE_PER_DAY


def test_the_bond_rate_is_locked_at_purchase(eco):
    eco.wallet.credit(1, 10_000, "テスト")
    set_rf(eco, rates.RF_MAX)  # 8%
    assert eco.bonds.bond_quote("normal", 10_000, NOW)["rate"] == pytest.approx(teiai.RF_MAX_PCT)
    assert eco.bonds.bond_buy(1, "normal", 10_000, NOW) == (True, "")
    set_rf(eco, rates.RF_MIN)  # あとで金利が下がっても、買ったときの 8% のまま
    done = eco.bonds.bond_settle(NOW + 3 * DAY)
    assert [(d["status"], d["paid"]) for d in done] == [("paid", 10_800)]
    assert eco.wallet.balance(1) == INITIAL_BALANCE + 10_000 + 800


def test_a_bond_bought_at_a_high_rate_sells_for_more_after_rates_fall(eco):
    eco.wallet.credit(1, 10_000, "テスト")
    set_rf(eco, rates.RF_MAX)
    eco.bonds.bond_buy(1, "normal", 10_000, NOW)
    row = eco.bonds.bond_list(1)["active"][0]
    later = NOW + DAY
    at_high = eco.bonds.bond_price(row, later)
    set_rf(eco, rates.RF_MIN)
    at_low = eco.bonds.bond_price(row, later)
    assert at_low > at_high and at_low > 10_000  # 金利が下がると、満期前でも値上がり益が出る
    ok, got = eco.bonds.bond_cancel(1, row["id"], later)
    assert ok and got == at_low
