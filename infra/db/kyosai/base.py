"""帝愛共済リポジトリの基礎部分: コンストラクタと口（持ち口）の管理・回収・引き戻し。"""

from domain import kyosai
from domain.ports.repos import IKyosaiRepo
from infra.db.account import AccountCore
from infra.db.bank import BankRepo
from infra.db.base import EcoDb, ScopedRepo
from infra.db.notices import NoticeRepo

DAY = 86400


class KyosaiRepo(ScopedRepo, IKyosaiRepo):
    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        bank: BankRepo,
        notices: NoticeRepo,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._bank = bank
        self._note = notices

    # 口

    def _holdings_for(self, guild_id: int, *, with_house: bool = True) -> list[tuple[int, int]]:
        rows = self._all(
            "SELECT user_id, units FROM kyosai_units WHERE guild_id=? AND units>0 ORDER BY user_id", (guild_id,)
        )
        players = [(r["user_id"], r["units"]) for r in rows]
        if not with_house:
            return players
        house = self._scalar("SELECT house_units FROM kyosai_state WHERE guild_id=?", (guild_id,))
        return [(kyosai.HOUSE, house), *players] if house and house > 0 else players

    def units_of_for(self, guild_id: int, user_id: int) -> int:
        if user_id == kyosai.HOUSE:
            v = self._scalar("SELECT house_units FROM kyosai_state WHERE guild_id=?", (guild_id,))
            return v if v is not None else 0
        v = self._scalar("SELECT units FROM kyosai_units WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        return v if v is not None else 0

    def _move_units_for(self, guild_id: int, src: int, dst: int, units: int, now: float) -> None:
        if src == kyosai.HOUSE:
            self._ensure_state_for(guild_id)
            self._conn.execute(
                "UPDATE kyosai_state SET house_units=house_units-? WHERE guild_id=?", (units, guild_id)
            )
        else:
            self._conn.execute(
                "UPDATE kyosai_units SET units=units-? WHERE guild_id=? AND user_id=?", (units, guild_id, src)
            )
        if dst == kyosai.HOUSE:
            self._ensure_state_for(guild_id)
            self._conn.execute(
                "UPDATE kyosai_state SET house_units=house_units+? WHERE guild_id=?", (units, guild_id)
            )
        else:
            self._conn.execute(
                "INSERT INTO kyosai_units (guild_id, user_id, units) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET units=units+excluded.units",
                (guild_id, dst, units),
            )

    def recover_units_for(self, guild_id: int, user_id: int, now: float) -> int:
        """その人の口を、すべて帝愛の持ち口へ回収する（破産・休眠）。回収した口数を返す。"""
        units = self.units_of_for(guild_id, user_id)
        if units > 0 and user_id != kyosai.HOUSE:
            self.bank_sync(guild_id, now)
            self._move_units_for(guild_id, user_id, kyosai.HOUSE, units, now)
            self._conn.execute(
                "UPDATE kyosai_offers SET status='cancelled' WHERE guild_id=? AND seller=? AND status='open'",
                (guild_id, user_id),
            )
        return units

    # 取引停止時の引き戻し（破産猶予入り・自己破産。#25 と同型の逃避をふさぐ）

    def prepare(self, guild_id: int, user_id: int, now: float) -> None:
        self._kyosai_pull_back_for(guild_id, user_id, now)

    def on_grace_enter(self, guild_id: int, user_id: int, now: float) -> None:
        self._kyosai_pull_back_for(guild_id, user_id, now)

    def _kyosai_pull_back_for(self, guild_id: int, user_id: int, now: float) -> int:
        """その人が最高入札者になっている進行中の競りをすべて取り消し、預かり金を手元へ戻す。引き戻した件数を返す。"""
        pulled = 0
        for row in self._all(
            "SELECT id, top_bid FROM kyosai_auctions WHERE guild_id=? AND status='open' AND top_bidder=?",
            (guild_id, user_id),
        ):
            if row["top_bid"] > 0:
                self._account.ensure(guild_id, user_id)
                self._account.apply(guild_id, user_id, row["top_bid"], "共済:競りの入札の返金（取引停止）")
            self._conn.execute("UPDATE kyosai_auctions SET top_bid=0, top_bidder=NULL WHERE id=?", (row["id"],))
            pulled += 1
        return pulled

    def _ensure_state_for(self, guild_id: int) -> None:
        self._conn.execute("INSERT OR IGNORE INTO kyosai_state (guild_id) VALUES (?)", (guild_id,))

    def bank_sync(self, guild_id: int, now: float) -> None:
        self._bank.bank_sync_for(guild_id, now)
        self._ensure_state_for(guild_id)
        self._ensure_supply_for(guild_id)

    def _ensure_supply_for(self, guild_id: int) -> None:
        """口がまだ1つもないサーバーでは、総口数をすべて帝愛の持ち口として用意する（競りで売り出す。#93）。"""
        house = self._scalar("SELECT house_units FROM kyosai_state WHERE guild_id=?", (guild_id,)) or 0
        circ = self._scalar("SELECT COALESCE(SUM(units), 0) FROM kyosai_units WHERE guild_id=?", (guild_id,)) or 0
        if house + circ <= 0:
            self._conn.execute(
                "UPDATE kyosai_state SET house_units=? WHERE guild_id=?", (kyosai.TOTAL_UNITS, guild_id)
            )

    def _gate_locked_for(self, guild_id: int, now: float) -> bool:
        theta = self._bank.bank_reserve_for(guild_id, now)[2]
        debtors = self._scalar(
            "SELECT COUNT(*) FROM teiai_grace WHERE guild_id=? AND grace_until IS NOT NULL", (guild_id,)
        )
        return kyosai.gate_locked(theta, debtors)
