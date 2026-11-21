"""会社（設立・出資・生産・出荷・増築・倒産）の永続化リポジトリ（SRP・Constructor DI）。

数字と決まりは `domain/company.py`、表は `infra/db/schema/tables.py` の `companies`。
設立費は帝愛銀行へ、建設費・維持費は帝愛の胴元の口座へ入る。出荷の代金は、まず胴元の口座から出て、足りない分は1日の買取枠（DAILY_GRANT）まで帝愛が新しく出す。
"""

from typing import Any, Optional

from domain import company as company_mod
from domain import teiai
from domain.errors import Failure, Outcome, fail
from domain.ports.bridges import (
    AssetSeizureStep,
    IBankCashFlow,
    IHouseAccount,
    INoticeSink,
    IStatusReader,
    LiquidationStep,
)
from domain.ports.repos import ICompanyRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


class CompanyRepo(ScopedRepo, ICompanyRepo):
    """会社の設立・資金の出し入れ・在庫の生産と出荷・維持費と倒産。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        status_reader: IStatusReader,
        bank: IBankCashFlow,
        house: IHouseAccount,
        notices: INoticeSink,
        tz_hours: float,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._tz_hours = (
            tz_hours  # 日付の切り替わり（維持費の精算日・その日の買取価格）を決める時差。IClock.offset_hours()
        )
        self._account = account
        self._status_reader = status_reader
        self._bank = bank
        self._house = house
        self._notice_sink = notices

    # ------------------------------------------------------------------ #
    # 読み書きの土台
    # ------------------------------------------------------------------ #
    def _load(self, guild_id: int, user_id: int) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM companies WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    def _save(self, c: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE companies SET kind=?, level=?, funds=?, stock=?, stock_ts=?, upkeep_day=?, unpaid_days=?, "
            "sold_total=? WHERE guild_id=? AND user_id=?",
            (
                c["kind"],
                c["level"],
                c["funds"],
                c["stock"],
                c["stock_ts"],
                c["upkeep_day"],
                c["unpaid_days"],
                c["sold_total"],
                c["guild_id"],
                c["user_id"],
            ),
        )

    def _settle(self, c: dict[str, Any], now: float) -> bool:
        """いまの時刻まで、生産と維持費を進めて保存する。倒産したら会社を消して False。"""
        gid = c["guild_id"]
        c["stock"] = company_mod.produce(c["kind"], c["level"], gid, c["stock"], c["stock_ts"], now, self._tz_hours)
        c["stock_ts"] = max(c["stock_ts"], now)
        today = company_mod.day_of(now, self._tz_hours)
        fee = company_mod.daily_upkeep(c["level"])
        while c["upkeep_day"] < today:
            c["upkeep_day"] = company_mod.next_day(c["upkeep_day"])
            if c["funds"] >= fee:
                c["funds"] -= fee
                c["unpaid_days"] = 0
                self._house.house_receive_for(gid, fee)
                continue
            c["unpaid_days"] += 1
            if c["unpaid_days"] >= company_mod.BANKRUPT_DAYS:
                self._house.house_receive_for(gid, c["funds"])  # 残った会社資金は、帝愛が引き取る
                self._conn.execute("DELETE FROM companies WHERE guild_id=? AND user_id=?", (gid, c["user_id"]))
                self._drop_stakes(gid, c["user_id"])  # 倒産すると、出資は戻らない
                self._notice_sink.note_for(gid, c["user_id"], "company_bankrupt", name=c["name"])
                return False
        self._save(c)
        return True

    def _current(self, guild_id: int, user_id: int, now: float) -> Optional[dict[str, Any]]:
        """精算済みの会社（無い・倒産したなら None）。トランザクションの中で呼ぶ。"""
        c = self._load(guild_id, user_id)
        if c is None or not self._settle(c, now):
            return None
        return c

    def _stakes(self, guild_id: int, owner_id: int) -> dict[int, int]:
        """{出資者: 出資額}。"""
        return {
            r["investor_id"]: r["amount"]
            for r in self._all(
                "SELECT investor_id, amount FROM company_stakes WHERE guild_id=? AND owner_id=?", (guild_id, owner_id)
            )
        }

    def _drop_stakes(self, guild_id: int, owner_id: int) -> None:
        self._conn.execute("DELETE FROM company_stakes WHERE guild_id=? AND owner_id=?", (guild_id, owner_id))

    def _budget_for(self, guild_id: int, day: str) -> int:
        """帝愛の買取予算（胴元の口座の残高と、今日の買取枠の残り）。"""
        grant = self._house.house_grant_left_for(guild_id, day, company_mod.DAILY_GRANT)
        return self._house.house_balance_for(guild_id) + grant

    def _view(self, c: dict[str, Any], now: float) -> dict[str, Any]:
        """画面に出す形（会社の行に、その日の状態と、計算で出る数字を足す）。"""
        state = company_mod.day_state(c["kind"], c["guild_id"], company_mod.day_of(now, self._tz_hours))
        stakes = self._stakes(c["guild_id"], c["user_id"])
        return {
            **c,
            "invested": company_mod.invested(c["level"]),
            "daily_output": company_mod.daily_output(c["kind"], c["level"]),
            "storage_cap": company_mod.storage_cap(c["kind"], c["level"]),
            "upkeep": company_mod.daily_upkeep(c["level"]),
            "upgrade_cost": company_mod.upgrade_cost(c["level"]),
            "condition": state.condition.key,
            "yield_mult": state.yield_mult,
            "price": state.price,
            "stock_value": int(c["stock"] * state.price),
            "budget": self._budget_for(c["guild_id"], company_mod.day_of(now, self._tz_hours)),
            "stakes_total": sum(stakes.values()),
            "stake_cap": company_mod.stake_cap(c["level"]),
            "investors": len(stakes),
            "withdrawable": max(0, c["funds"] - sum(stakes.values())),
        }

    # ------------------------------------------------------------------ #
    # 会社の操作
    # ------------------------------------------------------------------ #
    def company_get(self, user_id: int, now: float) -> Optional[dict[str, Any]]:
        """自分の会社（いまの時刻まで精算したもの）。無ければ None。"""
        with self._db.tx():
            c = self._current(self._g(), user_id, now)
            return self._view(c, now) if c else None

    def company_found(self, user_id: int, name: str, kind: str, now: float) -> Outcome[dict[str, Any]]:
        """会社を設立する（設立費と、Lv1 の建設費を払う）。(成功か, Failure か会社)。"""
        ok, checked = company_mod.check_name(name)
        if not ok:
            assert isinstance(checked, Failure)
            return False, checked
        assert isinstance(checked, str)
        name = checked
        if kind not in company_mod.KINDS:
            return False, fail("company_kind_unknown")
        cost = company_mod.FOUND_FEE + company_mod.BUILD_COSTS[0]
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            if self._current(gid, user_id, now) is not None:
                return False, fail("company_exists")
            if self._status_reader.status_for(gid, user_id, now) != teiai.KATAGI:
                return False, fail("company_katagi_only")
            bal = self._account.raw_balance(gid, user_id)
            if bal < cost:
                return False, fail("company_found_short", cost=cost, balance=bal)
            self._account.apply(gid, user_id, -cost, f"会社設立:{name}（設立費・建設費）")
            self._bank.bank_income_for(gid, company_mod.FOUND_FEE, now)
            self._house.house_receive_for(gid, company_mod.BUILD_COSTS[0])
            self._conn.execute(
                "INSERT INTO companies (guild_id, user_id, name, kind, stock_ts, upkeep_day, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, user_id, name, kind, now, company_mod.day_of(now, self._tz_hours), now),
            )
            c = self._load(gid, user_id)
            assert c is not None
            return True, self._view(c, now)

    def company_invest(self, user_id: int, amount: int, now: float) -> Outcome[int]:
        """個人の残高から、会社資金へ出資する。(成功か, Failure か会社資金)。"""
        if amount <= 0:
            return False, fail("company_amount_invalid")
        with self._db.tx():
            gid = self._g()
            c = self._current(gid, user_id, now)
            if c is None:
                return False, fail("company_none")
            if self._status_reader.status_for(gid, user_id, now) != teiai.KATAGI:
                return False, fail("company_katagi_only")  # 借金のある人が、会社資金へ逃がすのを防ぐ
            bal = self._account.raw_balance(gid, user_id)
            if bal < amount:
                return False, fail("insufficient_balance", balance=bal)
            self._account.apply(gid, user_id, -amount, f"会社へ出資:{c['name']}")
            c["funds"] += amount
            self._save(c)
            return True, c["funds"]

    def company_withdraw(self, user_id: int, amount: Optional[int] = None, now: Optional[float] = None) -> Outcome[int]:
        """会社資金を、個人の残高へ引き出す（amount が None なら全額）。(成功か, Failure か引き出した額)。"""
        now = self._now() if now is None else now
        if amount is not None and amount <= 0:
            return False, fail("company_amount_invalid")
        with self._db.tx():
            gid = self._g()
            c = self._current(gid, user_id, now)
            if c is None:
                return False, fail("company_none")
            # 出資者のお金は、社長が個人の残高へ引き出せない（増築や維持費には使える）
            locked = sum(self._stakes(gid, user_id).values())
            free = c["funds"] - locked
            if c["funds"] <= 0:
                return False, fail("company_no_funds")
            if free <= 0:
                return False, fail("company_funds_locked", locked=locked)
            take = free if amount is None else amount
            if take > free:
                if locked == 0:
                    return False, fail("company_funds_short", need=take, funds=c["funds"])
                return False, fail("company_withdraw_over", need=take, free=free, locked=locked)
            c["funds"] -= take
            self._save(c)
            self._account.apply(gid, user_id, take, f"会社から引き出し:{c['name']}")
            return True, take

    def company_ship(self, user_id: int, now: float) -> Outcome[dict[str, Any]]:
        """倉庫の在庫を、今日の相場で帝愛に売る。(成功か, Failure か {paid, pinhane, price, left})。"""
        with self._db.tx():
            gid = self._g()
            c = self._current(gid, user_id, now)
            if c is None:
                return False, fail("company_none")
            day = company_mod.day_of(now, self._tz_hours)
            price = company_mod.day_state(c["kind"], gid, day).price
            paid, pinhane, left = company_mod.sale(c["stock"], price, self._budget_for(gid, day))
            if paid <= 0:
                return False, fail("company_no_budget" if int(c["stock"] * price) > 0 else "company_nothing_to_ship")
            from_house = self._house.house_pay_for(gid, paid)
            self._house.house_grant_use_for(gid, day, paid - from_house)  # 口座で足りない分は、今日の買取枠から
            c["funds"] += paid
            c["stock"] = left
            c["sold_total"] += paid
            # 配当: 買取代金のうち配当率の分を、出資額の比で出資者の残高へ（会社資金から出す）
            shares = company_mod.dividend_split(paid, c.get("dividend_rate") or 0.0, self._stakes(gid, user_id))
            for investor_id, money in shares.items():
                c["funds"] -= money
                self._account.ensure(gid, investor_id)
                self._account.apply(gid, investor_id, money, f"配当:{c['name']}")
                self._conn.execute(
                    "UPDATE company_stakes SET dividends=dividends+? WHERE guild_id=? AND owner_id=? AND investor_id=?",
                    (money, gid, user_id, investor_id),
                )
            self._save(c)
            return True, {
                "paid": paid,
                "pinhane": pinhane,
                "price": price,
                "left": int(left * price),
                "dividends": sum(shares.values()),
            }

    def company_upgrade(self, user_id: int, now: float) -> Outcome[int]:
        """会社資金で、事業所を1レベル増築する。(成功か, Failure か新しいレベル)。"""
        with self._db.tx():
            gid = self._g()
            c = self._current(gid, user_id, now)
            if c is None:
                return False, fail("company_none")
            cost = company_mod.upgrade_cost(c["level"])
            if cost is None:
                return False, fail("company_max_level", max=company_mod.MAX_LEVEL)
            if c["funds"] < cost:
                return False, fail("company_funds_short", need=cost, funds=c["funds"])
            c["funds"] -= cost
            c["level"] += 1
            self._house.house_receive_for(gid, cost)
            self._save(c)
            return True, c["level"]

    def company_change_kind(self, user_id: int, kind: str, now: float) -> Outcome[str]:
        """事業所の種類を変える。それまでの建設費と在庫を失い、会社資金で Lv1 から建て直す。(成功か, Failure か空文字)。"""
        if kind not in company_mod.KINDS:
            return False, fail("company_kind_unknown")
        with self._db.tx():
            gid = self._g()
            c = self._current(gid, user_id, now)
            if c is None:
                return False, fail("company_none")
            if c["kind"] == kind:
                return False, fail("company_same_kind")
            cost = company_mod.BUILD_COSTS[0]
            if c["funds"] < cost:
                return False, fail("company_funds_short", need=cost, funds=c["funds"])
            c["funds"] -= cost
            c["kind"], c["level"], c["stock"], c["stock_ts"] = kind, 1, 0.0, now
            self._house.house_receive_for(gid, cost)
            self._save(c)
            return True, ""

    # ------------------------------------------------------------------ #
    # ほかの人の会社への出資（#71）
    # ------------------------------------------------------------------ #
    def stake_invest(self, investor_id: int, owner_id: int, amount: int, now: float) -> Outcome[dict[str, Any]]:
        """owner_id の会社へ出資する。お金は会社資金に入り、払い戻しはできない。(成功か, Failure か {amount, total})。"""
        if investor_id == owner_id:
            return False, fail("stake_self")
        if amount < company_mod.STAKE_MIN:
            return False, fail("stake_min", min=company_mod.STAKE_MIN)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, investor_id)
            c = self._current(gid, owner_id, now)
            if c is None:
                return False, fail("stake_no_company")
            if self._status_reader.status_for(gid, investor_id, now) != teiai.KATAGI:
                return False, fail("company_katagi_only")
            stakes = self._stakes(gid, owner_id)
            room = company_mod.stake_cap(c["level"]) - sum(stakes.values())
            if amount > room:
                return False, fail("stake_cap", room=max(0, room), cap=company_mod.stake_cap(c["level"]))
            bal = self._account.raw_balance(gid, investor_id)
            if bal < amount:
                return False, fail("insufficient_balance", balance=bal)
            self._account.apply(gid, investor_id, -amount, f"出資:{c['name']}")
            c["funds"] += amount
            self._save(c)
            self._conn.execute(
                "INSERT INTO company_stakes (guild_id, owner_id, investor_id, amount) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, owner_id, investor_id) DO UPDATE SET amount = amount + excluded.amount",
                (gid, owner_id, investor_id, amount),
            )
            return True, {"amount": amount, "total": stakes.get(investor_id, 0) + amount, "name": c["name"]}

    def stake_holdings(self, investor_id: int, now: float) -> list[dict[str, Any]]:
        """自分の出資先。[{owner_id, name, kind, amount, dividends, share, dividend_rate}]（share は持分の割合）。"""
        with self._db.tx():
            gid = self._g()
            rows = self._all(
                "SELECT owner_id, amount, dividends FROM company_stakes WHERE guild_id=? AND investor_id=?",
                (gid, investor_id),
            )
            out = []
            for r in rows:
                c = self._current(gid, r["owner_id"], now)
                if c is None:
                    continue  # 倒産していたら、精算で出資ごと消えている
                total = sum(self._stakes(gid, r["owner_id"]).values())
                out.append(
                    {
                        "owner_id": r["owner_id"],
                        "name": c["name"],
                        "kind": c["kind"],
                        "amount": r["amount"],
                        "dividends": r["dividends"],
                        "share": r["amount"] / total if total else 0.0,
                        "dividend_rate": c.get("dividend_rate") or 0.0,
                    }
                )
            return out

    def company_set_dividend(self, user_id: int, rate: float, now: float) -> Outcome[float]:
        """自分の会社の配当率を決める（0〜DIVIDEND_MAX）。(成功か, Failure か率)。"""
        if not 0.0 <= rate <= company_mod.DIVIDEND_MAX:
            return False, fail("dividend_rate_range", max=company_mod.DIVIDEND_MAX)
        with self._db.tx():
            gid = self._g()
            c = self._current(gid, user_id, now)
            if c is None:
                return False, fail("company_none")
            self._conn.execute(
                "UPDATE companies SET dividend_rate=? WHERE guild_id=? AND user_id=?", (rate, gid, user_id)
            )
            return True, rate

    def company_settle_all(self, now: float) -> list[dict[str, Any]]:
        """このサーバーの会社を、すべて精算する。倒産した会社 [{user_id, name}] を返す（告知用）。"""
        bankrupt = []
        with self._db.tx():
            gid = self._g()
            rows = self._all("SELECT * FROM companies WHERE guild_id=?", (gid,))
            for row in rows:
                c = dict(row)
                if not self._settle(c, now):
                    bankrupt.append({"user_id": c["user_id"], "name": c["name"]})
        return bankrupt

    # ------------------------------------------------------------------ #
    # 純資産・差し押さえ（ValuationSource / 強制執行・破産のステップから使う）
    # ------------------------------------------------------------------ #
    def collect_values(self, guild_id: int, totals: dict[int, int]) -> None:
        """純資産に数える額: 会社資金と、事業所（建設費の累計）。出資を受けていれば、社長と出資者で分ける。"""
        for r in self._all("SELECT user_id, funds, level FROM companies WHERE guild_id=?", (guild_id,)):
            owner_value, investors = company_mod.stake_values(
                r["funds"] + company_mod.invested(r["level"]), self._stakes(guild_id, r["user_id"])
            )
            totals[r["user_id"]] = totals.get(r["user_id"], 0) + owner_value
            for uid, v in investors.items():
                totals[uid] = totals.get(uid, 0) + v

    def _company_take_for(self, guild_id: int, user_id: int, need: int) -> int:
        """強制執行: 会社資金から need まで取り上げる。取り上げた額を返す。"""
        c = self._load(guild_id, user_id)
        taken = max(0, min(need, c["funds"])) if c else 0
        if c and taken:
            self._conn.execute(
                "UPDATE companies SET funds=funds-? WHERE guild_id=? AND user_id=?", (taken, guild_id, user_id)
            )
        return taken

    def _company_dissolve_for(self, guild_id: int, user_id: int) -> int:
        """自己破産: 会社をたたむ（事業所と在庫は失う）。残っていた会社資金を返す。"""
        c = self._load(guild_id, user_id)
        if c is None:
            return 0
        self._conn.execute("DELETE FROM companies WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        self._drop_stakes(guild_id, user_id)  # 社長の自己破産で会社がなくなると、出資も戻らない
        return c["funds"]


def company_seizure_step(account: AccountCore, company: CompanyRepo, notices: INoticeSink) -> AssetSeizureStep:
    """強制執行: 会社資金を差し押さえる（事業所には手を付けない）。"""

    def seize(guild_id: int, user_id: int, need: int, reason: str, now: float) -> int:
        taken = company._company_take_for(guild_id, user_id, need)
        if taken:
            account.ledger(
                guild_id,
                user_id,
                0,
                account.raw_balance(guild_id, user_id),
                f"強制執行:会社資金の差し押さえ（{taken:,}ペリカ）",
            )
            notices.note_for(guild_id, user_id, "seize_company", taken=taken)
        return taken

    return seize


def company_liquidation_step(account: AccountCore, company: CompanyRepo) -> LiquidationStep:
    """自己破産: 会社をたたみ、残っていた会社資金を現金に戻す（そのあと、債権者への配当に回る）。"""

    def liquidate(guild_id: int, user_id: int, now: float) -> None:
        saved = company._company_dissolve_for(guild_id, user_id)
        if saved:
            account.apply(guild_id, user_id, saved, "破産:会社資金の差し押さえ")

    return liquidate
