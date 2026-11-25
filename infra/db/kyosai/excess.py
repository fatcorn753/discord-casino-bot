"""超過損失の請求（ExcessLossSink）: 口数に応じた負担を手元と借金で徴収する。"""

from domain import kyosai

from .base import KyosaiRepo as Base


class KyosaiRepo(Base):
    def charge_excess_for(self, guild_id: int, amount: int, now: float) -> int:
        """超過損失を口数に応じて請求する。手元から取り、足りない分は借金にする。

        帝愛の持ち口の分は、誰も払わない（払われずに残る）。請求できた額（手元 + 借金）を返す。
        """
        collected = 0
        for user_id, share in kyosai.split(amount, self._holdings_for(guild_id)).items():
            if user_id == kyosai.HOUSE or share <= 0:
                continue
            self._account.ensure(guild_id, user_id)
            took = -self._account.apply(guild_id, user_id, -share, "共済:超過損失の負担", kind="kyosai_excess_charge")
            rest = share - took
            if rest > 0:
                self._account.add_debt_principal(guild_id, user_id, rest)
                self._account.ledger(  # 借金が増えたことを履歴に残す（手元は動かないので 0）
                    guild_id,
                    user_id,
                    0,
                    self._account.raw_balance(guild_id, user_id),
                    f"共済:超過損失の負担（借金 {rest:,}ペリカ）",
                    kind="kyosai_excess_charge",
                )
                self._account.after_debt_change(guild_id, user_id)
            self._note.note_for(guild_id, user_id, "kyosai_charged", amount=share, debt=rest)
            collected += share
        if collected:
            self._conn.execute(
                "INSERT INTO kyosai_state (guild_id, charged) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET charged=charged+excluded.charged",
                (guild_id, collected),
            )
        return collected
