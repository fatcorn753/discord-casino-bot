"""ペリカの残高・デイリー・ランキング・成績・履歴・送金（Economy に混ぜ込む部品）。"""

import datetime
import math
from typing import Optional

from domain import market as market_mod
from domain import teiai
from domain.errors import fail
from domain.shop import STREAK_GUARD
from domain.types import DailyResult, TransferResult
from infra.db.base import EcoBase


class WalletMixin(EcoBase):
    """残高操作・デイリーボーナス・ランキング・統計・履歴・送金。"""

    # ------------------------------------------------------------------ #
    # 残高                                                                #
    # ------------------------------------------------------------------ #
    def balance(self, user_id: int) -> int:
        with self._lock, self._conn:
            self._ensure(user_id)
            return self._balance(user_id)

    def credit(self, user_id: int, amount: int, reason: str) -> int:
        """残高を増やす（amount > 0）。管理者の付与などに使う。"""
        if amount <= 0:
            raise ValueError("amount は正の数にしてください")
        with self._lock, self._conn:
            self._ensure(user_id)
            self._apply(user_id, amount, reason)
            return self._balance(user_id)

    def set_balance(self, user_id: int, amount: int, reason: str) -> tuple:
        """残高を amount ちょうどにする（管理者用。差額は台帳に残る）。収入ではないので、天引きしない。

        amount が負のときは、手元を 0 にして、帝愛の借金を -amount ちょうどにする（借金状態にする。いまの借金は置き換わる）。
        0 以上のときは、借金には触れない。(変更前の残高, 変更後の残高, 変更前の借金, 変更後の借金) を返す。
        """
        with self._lock, self._conn:
            self._ensure(user_id)
            before, debt_before = self._balance(user_id), self._debt(user_id)
            target = max(0, amount)
            if target != before:
                self._apply(user_id, target - before, reason)
            if amount < 0 and -amount != debt_before:
                self._conn.execute(
                    "UPDATE users SET debt=? WHERE guild_id=? AND user_id=?", (-amount, self._g(), user_id)
                )
                self._ledger(user_id, 0, self._balance(user_id), f"{reason}（借金を {-amount:,}ペリカ に設定）")
                self._after_debt_change(user_id)
            return before, self._balance(user_id), debt_before, self._debt(user_id)

    def debit_up_to(self, user_id: int, amount: int, reason: str) -> int:
        """残高の範囲で amount を引く。実際に引けた額を返す（チンチロの倍払いなど）。"""
        if amount <= 0:
            return 0
        with self._lock, self._conn:
            self._ensure(user_id)
            return -self._apply(user_id, -amount, reason)

    # ------------------------------------------------------------------ #
    # デイリー                                                            #
    # ------------------------------------------------------------------ #
    def claim_daily(self, user_id: int, today: datetime.date, slot: Optional[int] = None) -> DailyResult:
        """時間帯（slot）ごとに1回のボーナス。slot が前回より進んでいれば受け取れる（省略時は1日1回）。

        連続日数は「日」で数える。昨日も受け取っていれば伸び、金額が増える。同じ日に何回受け取っても、連続日数は1日分。
        """
        slot = today.toordinal() if slot is None else slot
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._conn.execute(
                "SELECT daily_streak, last_daily, last_slot FROM users WHERE guild_id=? AND user_id=?",
                (self._g(), user_id),
            ).fetchone()
            last = datetime.date.fromisoformat(row["last_daily"]) if row["last_daily"] else None
            last_slot = row["last_slot"]
            if last_slot is None and last is not None:
                last_slot = last.toordinal()  # 時間帯を記録する前の受け取りは、その日の1回として扱う
            if (last is not None and last > today) or (last_slot is not None and slot <= last_slot):
                return DailyResult(
                    False, streak=row["daily_streak"], balance=self._balance(user_id)
                )  # 時計が戻っても二重に受け取れない
            guarded = 0
            if last == today:
                streak = max(1, row["daily_streak"])
            elif last == today - datetime.timedelta(days=1):
                streak = row["daily_streak"] + 1
            else:
                streak = 1
                # 受け取れなかった日が、連続守り券の枚数以内なら、1枚で1日ぶん埋めて連続をつなぐ（足りなければ1枚も使わない）
                missed = (today - last).days - 1 if last is not None else 0
                if missed > 0 and row["daily_streak"] > 0:
                    cur = self._conn.execute(
                        "UPDATE inventory SET qty = qty - ? WHERE guild_id=? AND user_id=? AND item_id=? AND qty>=?",
                        (missed, self._g(), user_id, STREAK_GUARD, missed),
                    )
                    if cur.rowcount:
                        streak, guarded = row["daily_streak"] + 1, missed
            amount = self.DAILY_BASE + self.DAILY_STREAK_BONUS * min(streak - 1, self.DAILY_STREAK_CAP)
            prof = self._conn.execute(
                "SELECT daily_boost FROM profile WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            boosted = bool(prof and prof["daily_boost"] > 0)
            if boosted:
                amount *= 2
                self._conn.execute(
                    "UPDATE profile SET daily_boost = daily_boost - 1 WHERE guild_id=? AND user_id=?",
                    (self._g(), user_id),
                )
            amount = round(amount * (1 + self._relic_buffs(user_id).get("daily", 0.0)))  # 発掘品（王家の黄金冠）の効果
            debt_before = self._debt(user_id)
            skimmed = teiai.skim(amount) if debt_before > 0 else 0  # 借金中は、帝愛がピンハネ（借金の元本は減らない）
            if skimmed:
                self._ledger(user_id, 0, self._balance(user_id), f"帝愛のピンハネ（デイリー {skimmed:,}ペリカ）")
                self._house_income(skimmed)  # 帝愛の胴元の口座に入る
            credit = amount - skimmed
            self._apply(
                user_id,
                credit,
                f"デイリー（{streak}日連続{'・2倍券' if boosted else ''}{'・連続守り券' + str(guarded) + '枚' if guarded else ''}）",
                income=credit,
            )
            repaid = debt_before - self._debt(user_id)
            seed = self._topup_seed(user_id)  # 借金中で、手元が種銭の枠に満たなければ、枠まで貸す
            self._conn.execute(
                "UPDATE users SET daily_streak=?, last_daily=?, last_slot=? WHERE guild_id=? AND user_id=?",
                (streak, today.isoformat(), slot, self._g(), user_id),
            )
            return DailyResult(True, amount, streak, self._balance(user_id), boosted, repaid, skimmed, seed, guarded)

    # ------------------------------------------------------------------ #
    # サーバー別ランキング用: どのサーバーで遊んだか                      #
    # ------------------------------------------------------------------ #
    def touch_guild_user(self, guild_id: int, user_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO guild_users (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id)
            )

    def guild_user_ids(self, guild_id: int) -> set:
        with self._lock:
            return {
                r["user_id"]
                for r in self._conn.execute("SELECT user_id FROM guild_users WHERE guild_id=?", (guild_id,))
            }

    # ------------------------------------------------------------------ #
    # ランキング・統計・履歴                                              #
    # ------------------------------------------------------------------ #
    def crypto_values(self) -> dict:
        """このサーバーの {ユーザーID: 現物（全銘柄）の評価額}。売ったときの手取りで、価格がまだない銘柄は数えない。"""
        with self._lock:
            prices = self.latest_prices()
            values = {}
            for r in self._conn.execute(
                "SELECT user_id, symbol, units FROM crypto WHERE guild_id=? AND units>0", (self._g(),)
            ):
                if r["symbol"] in prices:
                    values[r["user_id"]] = (
                        values.get(r["user_id"], 0) + market_mod.sell_quote(r["units"], prices[r["symbol"]])[0]
                    )
            return values

    def _ranked(self, user_ids: Optional[set]) -> list:
        """[(ユーザーID, 純資産)] を多い順に。純資産は、残高から借金を引き、CS・預金・債券・オプションの評価額を足したもの。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, balance - debt AS balance FROM users WHERE guild_id=?", (self._g(),)
            ).fetchall()
            values = self.crypto_values()
            for uid, eq in self.margin_equity().items():
                values[uid] = values.get(uid, 0) + eq
            for uid, held in self.bond_values().items():
                values[uid] = values.get(uid, 0) + held  # 預けている債券の元本も純資産
            for uid, held in self.bank_values().items():
                values[uid] = values.get(uid, 0) + held  # 帝愛への預金も純資産
            for uid, held in self.option_values().items():
                values[uid] = values.get(uid, 0) + held  # オプションも、いま売ったときの手取りで純資産
        total = [
            (r["user_id"], r["balance"] + values.get(r["user_id"], 0))
            for r in rows
            if user_ids is None or r["user_id"] in user_ids
        ]
        return sorted(total, key=lambda t: (-t[1], t[0]))

    def wealthy_ids(self) -> set:
        """富豪（純資産が WEALTHY_ASSETS 以上で、カタギ）のユーザーID。表示だけの身分で、ゲームの条件には使わない。"""
        return {
            uid
            for uid, total in self._ranked(None)
            if total >= teiai.WEALTHY_ASSETS and self.status(uid) == teiai.KATAGI
        }

    def display_status(self, user_id: int, wealthy: Optional[set] = None) -> str:
        """表示用の身分。status() と同じで、カタギで純資産が WEALTHY_ASSETS 以上なら WEALTHY。wealthy を渡すと再計算しない。"""
        status = self.status(user_id)
        if status != teiai.KATAGI:
            return status
        ids = self.wealthy_ids() if wealthy is None else wealthy
        return teiai.WEALTHY if user_id in ids else teiai.KATAGI

    def top(self, limit: int = 10, user_ids: Optional[set] = None) -> list:
        """[(順位, ユーザーID, 純資産), ...]。このサーバーの口座だけで並べる。user_ids を渡すと、さらにその人たちに絞る。

        純資産は、残高（現金）から借金を引き、CS・預金・債券・オプションの評価額を足したもの。トークンを買っても順位が下がらないようにしている。
        """
        return [(i + 1, uid, bal) for i, (uid, bal) in enumerate(self._ranked(user_ids)[:limit])]

    def rank_of(self, user_id: int, user_ids: Optional[set] = None) -> Optional[tuple]:
        """(順位, 純資産, 対象人数)。登録がなければ None。"""
        ranked = self._ranked(user_ids)
        for i, (uid, bal) in enumerate(ranked):
            if uid == user_id:
                return i + 1, bal, len(ranked)
        return None

    def peek_stats(self, user_id: int) -> Optional[dict]:
        """読み取り専用。未登録の人を作らずに、成績を返す（未登録なら None）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            return dict(row) if row else None

    def stats(self, user_id: int) -> dict:
        with self._lock, self._conn:
            self._ensure(user_id)
            self._accrue_debt_interest(user_id)  # 画面に出す借金に、帝愛の利息（トイチ）を含める
            return dict(
                self._conn.execute(
                    "SELECT * FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
                ).fetchone()
            )

    def history(self, user_id: int, limit: int = 10) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT delta, balance_after, reason, ts FROM ledger WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
                (self._g(), user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 送金                                                                #
    # ------------------------------------------------------------------ #
    def transfer(
        self,
        sender: int,
        receiver: int,
        amount: int,
        fee_rate: float,
        daily_limit: int,
        day_start_ts: float,
        min_games: int = 0,
    ) -> TransferResult:
        """sender から receiver へ送る。手数料は送る側が上乗せで払う。1日の送金額に上限がある。"""
        if sender == receiver:
            return TransferResult(False, fail("transfer_self"))
        if amount <= 0:
            return TransferResult(False, fail("amount_must_be_positive"))
        fee = (
            math.ceil(round(amount * fee_rate, 6)) if fee_rate else 0
        )  # 切り上げ（少額でも手数料は最低1）。1日の上限には数えない
        with self._lock, self._conn:
            self._ensure(sender)
            self._ensure(receiver)
            sent_today = -(
                self._conn.execute(
                    "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger WHERE guild_id=? AND user_id=? AND reason LIKE '送金（送）%' AND ts>=?",
                    (self._g(), sender, day_start_ts),
                ).fetchone()["s"]
            )
            if (
                self._conn.execute(
                    "SELECT games_played FROM users WHERE guild_id=? AND user_id=?", (self._g(), sender)
                ).fetchone()["games_played"]
                < min_games
            ):
                return TransferResult(False, fail("transfer_min_games", min_games=min_games))
            if sent_today + amount > daily_limit:
                return TransferResult(
                    False, fail("transfer_daily_limit", daily_limit=daily_limit, sent_today=sent_today)
                )
            if self._debt(sender) > 0:
                return TransferResult(False, fail("debt_blocks_transfer", debt=self._debt(sender)))
            if self._balance(sender) < amount + fee:
                return TransferResult(False, fail("transfer_insufficient_balance", amount=amount, fee=fee))
            self._apply(
                sender, -amount, f"送金（送）→{receiver}"
            )  # 手数料は別の行にする（1日の上限は、送った額だけで数えるため）
            if fee:
                self._apply(sender, -fee, "送金手数料")
            self._apply(receiver, amount, f"送金（受）←{sender}", income=amount)
            return TransferResult(True, received=amount, fee=fee, sender_balance=self._balance(sender))
