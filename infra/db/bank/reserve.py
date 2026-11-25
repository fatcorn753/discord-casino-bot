"""準備率と引当金のロック、銀行の状態（準備率 θ の計算と表示）。"""

from domain import rates, teiai, thermostat
from domain.ports.contracts import BankStatus

from .deposits import BankRepo as Base


class BankRepo(Base):
    def _bonds_outstanding_for(self, guild_id: int) -> int:
        """帝愛社債（保有中の債券）の元本残高。銀行の負債として準備率に数える（#35）。"""
        return self._scalar(
            "SELECT COALESCE(SUM(principal), 0) FROM bonds WHERE guild_id=? AND status='active'", (guild_id,)
        )

    def bank_locks_for(self, guild_id: int, now: float) -> float:
        """引当金のロック額: 破産猶予中の人への CDS の配当見込み（対象の借金の上限つき）+ 借金のある人の借金の LOCK_DEBT_RATE。"""
        locks = 0.0
        for r in self._all(
            "SELECT d.user_id, d.debt, g.grace_until FROM teiai_debts d "
            "LEFT JOIN teiai_grace g ON g.guild_id=d.guild_id AND g.user_id=d.user_id "
            "WHERE d.guild_id=? AND d.debt>0",
            (guild_id,),
        ):
            locks += teiai.LOCK_DEBT_RATE * r["debt"]
            if r["grace_until"] is not None:
                claims = self._scalar(
                    "SELECT COALESCE(SUM(stake * odds), 0) FROM cds WHERE guild_id=? AND target_id=? AND buyer_id!=target_id "
                    "AND status='active'",
                    (guild_id, r["user_id"]),
                )
                locks += min(claims, teiai.CDS_PAYOUT_CAP_RATE * r["debt"])
        return locks

    def _reserve_for(self, guild_id: int, cash: float, now: float) -> tuple[int, int, float, float, float | None]:
        """(信託総額, 社債の元本残高, 引当金, 利用可能現金, 準備率 θ)。θ = 利用可能現金 ÷ (信託 + 社債)（どちらもなければ None）。"""
        total = sum(a for _, a in self._deposits_for(guild_id))
        bonds = self._bonds_outstanding_for(guild_id)
        locks = self.bank_locks_for(guild_id, now)
        available = cash - locks
        liabilities = total + bonds  # 銀行が返す約束をしている額（信託と、帝愛社債の元本）
        return total, bonds, locks, available, (available / liabilities if liabilities > 0 else None)

    def bank_status(self, now: float) -> BankStatus:
        """銀行の状態（表示用）。available は利用可能現金、ratio は available ÷ (信託総額 + 社債の元本残高)（どちらもなければ None）。"""
        with self._db.tx():
            gid = self._g()
            state = self.bank_sync_for(gid, now)
            total, bonds, locks, available, ratio = self._reserve_for(gid, state["cash"], now)
            liabilities = total + bonds
            if available <= 0 and liabilities > 0:
                level = "frozen"
            elif ratio is None or ratio >= teiai.RUN_WARN_RATIO:
                level = "safe"
            elif ratio >= teiai.RUN_DANGER_RATIO:
                level = "warn"
            else:
                level = "danger"
            thermo = self._thermo_state_for(gid, now)
            th = self._one("SELECT burned, bailed FROM teiai_thermostat WHERE guild_id=?", (gid,))
            tr = self._one("SELECT paid, excess_unpaid FROM teiai_trust WHERE guild_id=?", (gid,))
            burned = th["burned"] if th is not None else 0.0
            bailed = th["bailed"] if th is not None else 0.0
            trust_paid = tr["paid"] if tr is not None else 0.0
            excess_unpaid = tr["excess_unpaid"] if tr is not None else 0.0
            return {
                "cash": state["cash"],
                "loaned": state["loaned"],
                "deposits": total,
                "bonds": bonds,
                "locks": locks,
                "available": available,
                "ratio": ratio,
                "level": level,
                "thermostat": {
                    "enforce": thermostat.ENFORCE,
                    "load": thermo.load,
                    "capacity": thermo.capacity,
                    "room": max(thermo.capacity - thermostat.COUPLING * thermo.load, 0.0) / thermostat.COUPLING,
                    "burned": burned,
                    "bailed": bailed,
                },
                "trust_rate": self.trust_rate_per_day_for(gid, now),
                "risk_free": self.risk_free_per_day_for(gid, now),
                "rf_target": rates.rf_target(ratio),
                "trust_paid": trust_paid,
                "excess_unpaid": excess_unpaid,
            }
