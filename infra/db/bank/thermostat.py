"""最前線サーモスタット（熱浴）: 流入の吸収・流出の肩代わりの内部状態。"""

from domain import thermostat

from .base import BankRepo as Base


class BankRepo(Base):
    def _thermo_state_for(self, guild_id: int, now: float) -> thermostat.State:
        """いまの時刻まで冷ました状態（銀行の行がある前提。bank_sync_for のあとで呼ぶ）。"""
        row = self._one(
            "SELECT thermo_load, thermo_capacity, thermo_ts FROM teiai_bank WHERE guild_id=?",
            (guild_id,),
        )
        assert row is not None  # bank_sync_for のあとで呼ぶので、銀行の行はある
        state = thermostat.State(load=row["thermo_load"], capacity=max(row["thermo_capacity"], thermostat.E_MIN))
        return thermostat.decay(state, (now - row["thermo_ts"]) / 86400)

    def _thermo_save_for(
        self, guild_id: int, state: thermostat.State, now: float, burned: float = 0.0, bailed: float = 0.0
    ) -> None:
        # bailout_debt 列は #32 の見直しで使わなくなった（列だけ残している）
        self._conn.execute(
            "UPDATE teiai_bank SET thermo_load=?, thermo_capacity=?, thermo_ts=?, "
            "thermo_burned=thermo_burned+?, thermo_bailed=thermo_bailed+? WHERE guild_id=?",
            (state.load, state.capacity, now, burned, bailed, guild_id),
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
