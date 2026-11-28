"""最前線サーモスタット（熱浴）: 流入の吸収・流出の肩代わりの内部状態。"""

from domain import thermostat

from .base import BankRepo as Base


class BankRepo(Base):
    def _thermo_state_for(self, guild_id: int, now: float) -> thermostat.State:
        """いまの時刻まで冷ました状態（銀行の行がある前提。bank_sync_for のあとで呼ぶ）。"""
        row = self._one(
            "SELECT load, capacity, ts FROM teiai_thermostat WHERE guild_id=?",
            (guild_id,),
        )
        if row is None:
            return thermostat.State(load=0.0, capacity=thermostat.E_MIN)
        state = thermostat.State(load=row["load"], capacity=max(row["capacity"], thermostat.E_MIN))
        return thermostat.decay(state, (now - row["ts"]) / 86400)

    def _thermo_save_for(
        self, guild_id: int, state: thermostat.State, now: float, burned: float = 0.0, bailed: float = 0.0
    ) -> None:
        self._conn.execute(
            "INSERT INTO teiai_thermostat (guild_id, load, capacity, ts, burned, bailed) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET load=excluded.load, capacity=excluded.capacity, ts=excluded.ts, "
            "burned=burned+excluded.burned, bailed=bailed+excluded.bailed",
            (guild_id, state.load, state.capacity, now, burned, bailed),
        )

    def _thermo_inflow_for(self, guild_id: int, amount: int, now: float) -> float:
        """流入を通し、銀行に入れてよい額を返す（観測だけの間は、全額）。"""
        state, net, burn = thermostat.absorb_inflow(self._thermo_state_for(guild_id, now), amount)
        self._thermo_save_for(guild_id, state, now, burned=burn)
        return net if thermostat.ENFORCE else float(amount)

    def _thermo_shock_for(self, guild_id: int, amount: int, now: float) -> float:
        """流出（損失・通常の支払い）を通し、熱浴から銀行へ注入する額を返す（観測だけの間は、0）。"""
        state, _effective, bailout = thermostat.absorb_shock(self._thermo_state_for(guild_id, now), amount)
        self._thermo_save_for(guild_id, state, now, bailed=bailout)
        return bailout if thermostat.ENFORCE else 0.0

    def bank_thermo_batch_for(self, guild_id: int, net: float, now: float) -> None:
        """流入と流出が混ざるバッチの純額 net（流入がプラス）を、サーモスタットに1回だけ通す。

        流入と流出は通す順番で結果が変わるため、バッチの中の個々の入出金は thermo=False で動かし、
        ここで純額だけを通す。銀行の現金は、burn の分だけ減り、bailout の分だけ増える（観測だけの間は動かさない）。
        純額が流出なら支払いの前に、流入なら入金のあとに呼ぶ（現金が一時的にマイナスにならないように）。
        """
        if net == 0:
            return
        state = self.bank_sync_for(guild_id, now)
        thermo, burn, bailout = thermostat.absorb_net(self._thermo_state_for(guild_id, now), net)
        self._thermo_save_for(guild_id, thermo, now, burned=burn, bailed=bailout)
        if thermostat.ENFORCE:
            self.bank_set_for(guild_id, max(state["cash"] - burn, 0.0) + bailout, state["loaned"])
