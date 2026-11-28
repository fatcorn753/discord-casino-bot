"""銀行の入出金: 手元現金・信託第一損失・熱浴肩代わり・共済負担のウォーターフォール。"""

import math

from .reserve import BankRepo as Base


class BankRepo(Base):
    def bank_income_for(
        self,
        guild_id: int,
        amount: int,
        now: float,
        *,
        thermo: bool = True,
    ) -> None:
        """銀行に amount が入る（手元の現金へ）。信託への分配はしない（信託は、決まった利息を受け取る。#33）。

        信託の残高が 0 でも入れる（社債の償還と共済の配当の原資になるため）。
        thermo=False は、バッチの純額を bank_thermo_batch_for や bank_pay_batch_for で別に通すときだけ使う。
        """
        if amount <= 0:
            return
        state = self.bank_sync_for(guild_id, now)
        received = self._thermo_inflow_for(guild_id, amount, now) if thermo else float(amount)
        self.bank_set_for(guild_id, state["cash"] + received, state["loaned"])

    def bank_pay_for(self, guild_id: int, amount: int, now: float, *, thermo: bool = True) -> int:
        """銀行が amount を払う。払えた額を返す。

        払う順番（ウォーターフォール。#33）:
        1. 手元の現金
        2. 帝愛信託（第一損失）: 足りない分を、信託の残高に応じて按分して減らす。信託はマイナスにはならない
        3. 信託でも足りない超過損失だけを熱浴へ: サーモスタットで「熱浴の肩代わり」と「共済の負担」に分ける

        サーモスタットに流出として通すのは「現金で払った分 + 超過損失」だけ（信託の取り崩しで、負側の器を使わない）。
        損失の支払いも、プレミアムなどの通常の支払いも、同じ流出として扱う。
        thermo=False は、サーモスタットに通さない（いまは使っていない。バッチは bank_pay_batch_for で通す）。
        """
        return self._outflow_for(guild_id, amount, now, use_trust=True, thermo=thermo)

    def bank_pay_batch_for(self, guild_id: int, amounts: list[int], now: float, *, offset: float = 0.0) -> list[int]:
        """入金と支払いが混ざるバッチの支払い amounts を、まとめて1回のウォーターフォールで払う。払えた額を、それぞれ返す。

        offset は、このバッチの流入（現金は動かさない。入金は先に thermo=False で済ませておく）。サーモスタットには
        「offset − 支払総額」の経済的な純額を1回だけ通す。信託の取り崩し分は負側の器にも流入側にも数えないので、
        ジュニアの信託が第一損失を負ってから、超過損失の分だけ熱浴が肩代わりする（#33）。
        払い切れないとき（帝愛の持ち口の分が残ったとき）は、払えた額を額に応じて按分する。
        """
        total = sum(a for a in amounts if a > 0)
        if total <= 0:
            if offset:
                self.bank_thermo_batch_for(guild_id, offset, now)
            return [0 for _ in amounts]
        paid = self._outflow_for(guild_id, total, now, use_trust=True, thermo=True, offset=offset)
        if paid >= total:
            return [max(0, a) for a in amounts]
        out = [max(0, a) * paid // total for a in amounts]
        for i in sorted(range(len(amounts)), key=lambda i: -(max(0, amounts[i]) * paid % total))[: paid - sum(out)]:
            out[i] += 1
        return out

    def _outflow_for(
        self, guild_id: int, amount: int, now: float, *, use_trust: bool, thermo: bool, offset: float = 0.0
    ) -> int:
        """bank_pay_for の本体。use_trust=False は、信託への利息のように、信託から取ると自分で自分に払うことになる流出。

        offset は、同じバッチの流入（bank_pay_batch_for）。サーモスタットには offset との純額を通す。
        """
        if amount <= 0:
            return 0
        state = self.bank_sync_for(guild_id, now)
        cash_pay = min(float(amount), max(state["cash"], 0.0))
        cash = state["cash"] - cash_pay
        shortfall = amount - cash_pay
        trust_take = self._trust_haircut_for(guild_id, shortfall) if use_trust and shortfall > 0 else 0
        excess = max(0.0, shortfall - trust_take)  # 信託でも受け止めきれない超過損失 Δraw
        bailout = 0.0
        if thermo:
            net = offset - amount  # このバッチの経済的な純額（流入がプラス、流出がマイナス）
            if net > 0:  # 純額が流入: 入れてよい額を超えた分（burn）は、手元から消える（マイナスにはしない）
                burn = net - self._thermo_inflow_for(guild_id, int(round(net)), now)
                cash = max(cash - burn, 0.0)
            else:
                # 信託の取り崩し分は負側の器を使わない（0 を超えて流入側へ反転させない）
                shock = max(0.0, -net - trust_take)
                if shock > 0:
                    bailout = self._thermo_shock_for(guild_id, int(round(shock)), now)
        cover = min(excess, bailout)  # 熱浴の肩代わりは、まず超過損失の穴埋めに使う
        cash += bailout - cover  # 余った肩代わりは、手元へ
        # 残りの超過損失は帝愛共済の口の持ち主の負担（#34）。帝愛の持ち口の分は払われずに残る
        charged = 0
        left = int(excess - cover)
        if left > 0:
            charged = min(left, max(0, self._excess_sink(guild_id, left, now)))
            if left > charged:
                self._conn.execute(
                    "INSERT INTO teiai_trust (guild_id, excess_unpaid) VALUES (?, ?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET excess_unpaid=excess_unpaid+excluded.excess_unpaid",
                    (guild_id, left - charged),
                )
        self.bank_set_for(guild_id, cash, state["loaned"])
        return min(amount, int(round(cash_pay + trust_take + cover + charged)))

    def _trust_haircut_for(self, guild_id: int, need: float) -> int:
        """帝愛信託の残高を、按分して need まで減らす（第一損失）。減らした額を返す（信託の総額が上限）。"""
        deposits = self._deposits_for(guild_id)
        total = sum(a for _, a in deposits)
        if total <= 0:
            return 0
        take = min(math.ceil(need - 1e-9), total)  # 払う側に端数の不足を出さないよう、切り上げて取る
        factor = max(0.0, 1 - take / total)
        for uid, amt in deposits:
            self._conn.execute(
                "UPDATE teiai_deposits SET amount=? WHERE guild_id=? AND user_id=?",
                (int(amt * factor), guild_id, uid),
            )
        return take
