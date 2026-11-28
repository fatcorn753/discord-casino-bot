"""帝愛物産（`/会社` の生産・出荷買取専用プールと日次買取枠）。"""

from domain.ports.contracts import HouseAccount

from .interest import BankRepo as Base


class BankRepo(Base):
    def house_income_for(self, guild_id: int, amount: int) -> None:
        """買取プールに amount を入れる（呼び出し側のトランザクションの中で使う。後方互換用）。"""
        if amount <= 0:
            return
        self._conn.execute(
            "INSERT INTO teiai_house (guild_id, balance, skimmed) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET balance=balance+excluded.balance, skimmed=skimmed+excluded.skimmed",
            (guild_id, amount, amount),
        )

    def house_balance_for(self, guild_id: int) -> int:
        v = self._scalar("SELECT balance FROM teiai_house WHERE guild_id=?", (guild_id,))
        return v if v is not None else 0

    def house_receive_for(self, guild_id: int, amount: int) -> None:
        """帝愛物産の買取プールに amount を入れる（設立費・建設費・維持費など）。"""
        if amount <= 0:
            return
        self._conn.execute(
            "INSERT INTO teiai_house (guild_id, balance, skimmed) VALUES (?, ?, 0) "
            "ON CONFLICT(guild_id) DO UPDATE SET balance=balance+excluded.balance",
            (guild_id, amount),
        )

    def house_pay_for(self, guild_id: int, amount: int) -> int:
        """帝愛物産の買取プールから amount を払う。残高を超えては払わない。払えた額を返す。"""
        paid = max(0, min(amount, self.house_balance_for(guild_id)))
        if paid:
            self._conn.execute("UPDATE teiai_house SET balance=balance-? WHERE guild_id=?", (paid, guild_id))
        return paid

    def house_grant_left_for(self, guild_id: int, day: str, cap: int) -> int:
        """会社の買取枠（買取プールが足りないとき、帝愛が新しく出す分）の、day の残り。1日の上限は cap。"""
        used = self._scalar("SELECT used FROM teiai_house_grants WHERE guild_id=? AND day=?", (guild_id, day))
        return max(0, cap - (used or 0))

    def house_grant_use_for(self, guild_id: int, day: str, amount: int) -> None:
        """買取枠から amount を出したと記録する。"""
        if amount <= 0:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO teiai_house (guild_id) VALUES (?)", (guild_id,)
        )  # teiai_house_grants の FK（guild_id → teiai_house.guild_id）を常に満たすため親行を先に作る
        self._conn.execute(
            "INSERT INTO teiai_house_grants (guild_id, day, used) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, day) DO UPDATE SET used=used+excluded.used",
            (guild_id, day, amount),
        )

    def house_account(self) -> HouseAccount:
        """帝愛物産の買取プール {balance: いまの残高, skimmed: 累計（後方互換用）}。読み取りだけ（なければ 0）。"""
        with self._lock:
            row = self._one("SELECT balance, skimmed FROM teiai_house WHERE guild_id=?", (self._g(),))
        return {"balance": row["balance"], "skimmed": row["skimmed"]} if row else {"balance": 0, "skimmed": 0}
