"""ペリカの残高・デイリー・ランキング・成績・履歴・送金リポジトリ。"""

import datetime
import math

from domain import teiai
from domain.errors import fail
from domain.ports.bridges import (
    IBankCashFlow,
    IDebtOps,
    IStatusReader,
    ValuationSource,
)
from domain.ports.contracts import LedgerEntry, RankEntry, UserRow
from domain.ports.repos import IWalletRepo
from domain.shop import STREAK_GUARD
from domain.types import DAILY_BASE, DAILY_STREAK_BONUS, DAILY_STREAK_CAP, DailyResult, TransferResult
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.inventory import InventoryCore
from infra.db.profile import ProfileCore
from infra.db.relics import RelicRepo

_SELECT_USER_ROW = (
    "SELECT a.guild_id, a.user_id, a.balance, "
    "COALESCE(ud.streak, 0) AS daily_streak, ud.last_daily, "
    "COALESCE(ps.games_played, 0) AS games_played, "
    "COALESCE(ps.total_wagered, 0) AS total_wagered, "
    "COALESCE(ps.total_won, 0) AS total_won, "
    "COALESCE(ps.biggest_win, 0) AS biggest_win, "
    "a.created_at, ud.last_slot, "
    "COALESCE(td.debt, 0) AS debt, "
    "COALESCE(td.principal, 0) AS debt_principal, "
    "COALESCE(tg.penalty, 0.0) AS grace_penalty, "
    "COALESCE(tg.penalty_at, 0.0) AS grace_penalty_at, "
    "tb.bankrupt_at, tb.blacklist_until "
    "FROM accounts a "
    "LEFT JOIN user_daily ud ON ud.guild_id = a.guild_id AND ud.user_id = a.user_id "
    "LEFT JOIN user_play_stats ps ON ps.guild_id = a.guild_id AND ps.user_id = a.user_id "
    "LEFT JOIN teiai_debts td ON td.guild_id = a.guild_id AND td.user_id = a.user_id "
    "LEFT JOIN teiai_grace tg ON tg.guild_id = a.guild_id AND tg.user_id = a.user_id "
    "LEFT JOIN teiai_bankruptcies tb ON tb.guild_id = a.guild_id AND tb.user_id = a.user_id "
    "WHERE a.guild_id=? AND a.user_id=?"
)


class WalletRepo(ScopedRepo, IWalletRepo):
    """残高操作・デイリーボーナス・ランキング・統計・履歴・送金。"""

    DAILY_BASE = DAILY_BASE
    DAILY_STREAK_BONUS = DAILY_STREAK_BONUS
    DAILY_STREAK_CAP = DAILY_STREAK_CAP

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        bank: IBankCashFlow,
        debt: IDebtOps,
        status_reader: IStatusReader,
        relics: RelicRepo,
        valuation_source: ValuationSource,
        guild_id: int = 0,
        strict: bool = False,
        *,
        inventory: InventoryCore | None = None,
        profile: ProfileCore | None = None,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._bank = bank
        self._debt = debt
        self._status_reader = status_reader
        self._relics = relics
        self._valuation_source: ValuationSource = valuation_source
        self._inventory = inventory or InventoryCore(db)
        self._profile = profile or ProfileCore(db)

    # ------------------------------------------------------------------ #
    # 残高                                                                #
    # ------------------------------------------------------------------ #
    def balance(self, user_id: int) -> int:
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            return self._account.raw_balance(gid, user_id)

    def credit(self, user_id: int, amount: int, reason: str) -> int:
        """残高を増やす（amount > 0）。管理者の付与などに使う。"""
        if amount <= 0:
            raise ValueError("amount は正の数にしてください")
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            self._account.apply(gid, user_id, amount, reason)
            return self._account.raw_balance(gid, user_id)

    def set_balance(self, user_id: int, amount: int, reason: str) -> tuple[int, int, int, int]:
        """残高を amount ちょうどにする（管理者用。差額は台帳に残る）。収入ではないので、天引きしない。

        amount が負のときは、手元を 0 にして、帝愛の借金を -amount ちょうどにする（借金状態にする。いまの借金は置き換わる）。
        0 以上のときは、借金には触れない。(変更前の残高, 変更後の残高, 変更前の借金, 変更後の借金) を返す。
        """
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            before, debt_before = self._account.raw_balance(gid, user_id), self._account.debt(gid, user_id)
            target = max(0, amount)
            if target != before:
                self._account.apply(gid, user_id, target - before, reason)
            if amount < 0 and -amount != debt_before:
                self._account.set_debt(gid, user_id, -amount, -amount, 0.0)
                self._account.ledger(
                    gid,
                    user_id,
                    0,
                    self._account.raw_balance(gid, user_id),
                    f"{reason}（借金を {-amount:,}ペリカ に設定）",
                )
                self._account.after_debt_change(gid, user_id)
            return (
                before,
                self._account.raw_balance(gid, user_id),
                debt_before,
                self._account.debt(gid, user_id),
            )

    def debit_up_to(self, user_id: int, amount: int, reason: str) -> int:
        """残高の範囲で amount を引く。実際に引けた額を返す（チンチロの倍払いなど）。"""
        if amount <= 0:
            return 0
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            return -self._account.apply(gid, user_id, -amount, reason)

    # ------------------------------------------------------------------ #
    # デイリー                                                            #
    # ------------------------------------------------------------------ #
    def claim_daily(self, user_id: int, today: datetime.date, slot: int | None = None) -> DailyResult:
        """時間帯（slot）ごとに1回のボーナス。slot が前回より進んでいれば受け取れる（省略時は1日1回）。

        連続日数は「日」で数える。昨日も受け取っていれば伸び、金額が増える。同じ日に何回受け取っても、連続日数は1日分。
        """
        slot = today.toordinal() if slot is None else slot
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            row = self._one(
                "SELECT streak AS daily_streak, last_daily, last_slot FROM user_daily WHERE guild_id=? AND user_id=?",
                (gid, user_id),
            )
            cur_streak = row["daily_streak"] if row is not None else 0
            last = datetime.date.fromisoformat(row["last_daily"]) if row and row["last_daily"] else None
            last_slot = row["last_slot"] if row is not None else None
            if last_slot is None and last is not None:
                last_slot = last.toordinal()  # 時間帯を記録する前の受け取りは、その日の1回として扱う
            if (last is not None and last > today) or (last_slot is not None and slot <= last_slot):
                return DailyResult(
                    False, streak=cur_streak, balance=self._account.raw_balance(gid, user_id)
                )  # 時計が戻っても二重に受け取れない
            if last == today:
                streak, guarded = max(1, cur_streak), 0
            elif last == today - datetime.timedelta(days=1):
                streak, guarded = cur_streak + 1, 0
            else:
                # 受け取れなかった日が守り券の枚数以内なら、1枚で1日ぶん埋めて連続をつなぐ（足りなければ1枚も使わない）
                missed = (today - last).days - 1 if last is not None else 0
                streak, guarded = 1, 0
                if missed > 0 and cur_streak > 0:
                    streak, guarded = (
                        (cur_streak + 1, missed) if self._inventory.consume(gid, user_id, STREAK_GUARD, missed) else (1, 0)
                    )
            amount = self.DAILY_BASE + self.DAILY_STREAK_BONUS * min(streak - 1, self.DAILY_STREAK_CAP)
            boosted = self._profile.consume_buff(gid, user_id, "daily_boost")
            if boosted:
                amount *= 2
            amount = round(
                amount * (1 + self._relics.relic_buffs_for(gid, user_id).get("daily", 0.0))
            )  # 発掘品（王家の黄金冠）の効果
            debt_before = self._account.debt(gid, user_id)
            skimmed = teiai.skim(amount) if debt_before > 0 else 0  # 借金中は、帝愛がピンハネ（借金の元本は減らない）
            if skimmed:
                self._account.ledger(
                    gid,
                    user_id,
                    0,
                    self._account.raw_balance(gid, user_id),
                    f"帝愛のピンハネ（デイリー {skimmed:,}ペリカ）",
                )
                self._bank.bank_income_for(gid, skimmed, self._now())  # 帝愛銀行へ入る（サーモスタット経由）
            credit = amount - skimmed
            self._account.apply(
                gid,
                user_id,
                credit,
                f"デイリー（{streak}日連続{'・2倍券' if boosted else ''}{'・連続守り券' + str(guarded) + '枚' if guarded else ''}）",
                income=credit,
            )
            repaid = debt_before - self._account.debt(gid, user_id)
            seed = self._debt.topup_seed_for(gid, user_id)  # 借金中で、手元が種銭の枠に満たなければ、枠まで貸す
            self._conn.execute(
                "INSERT INTO user_daily (guild_id, user_id, streak, last_daily, last_slot) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "streak=excluded.streak, last_daily=excluded.last_daily, last_slot=excluded.last_slot",
                (gid, user_id, streak, today.isoformat(), slot),
            )
            return DailyResult(
                True,
                amount,
                streak,
                self._account.raw_balance(gid, user_id),
                boosted,
                repaid,
                skimmed,
                seed,
                guarded,
            )

    # ------------------------------------------------------------------ #
    # ランキング・統計・履歴                                              #
    # ------------------------------------------------------------------ #
    def _ranked(self, guild_id: int, user_ids: set[int] | None) -> list[tuple[int, int]]:
        """[(ユーザーID, 金融純資産)] を多い順に。金融純資産は、残高から借金を引き、CS・預金・債券・オプションの評価額を足したもの。"""
        with self._db.tx():
            targets = (
                [
                    r["user_id"]
                    for r in self._all("SELECT user_id FROM teiai_debts WHERE guild_id=? AND debt>0", (guild_id,))
                ]
                if user_ids is None
                else user_ids
            )
            for uid in targets:
                # 残高表示と同じく、金融純資産にも溜まった利息（トイチ）を反映する
                self._debt.accrue_debt_interest_for(guild_id, uid, self._now())
            rows = self._all(
                "SELECT a.user_id, a.balance - COALESCE(d.debt, 0) AS balance "
                "FROM accounts a LEFT JOIN teiai_debts d ON d.guild_id=a.guild_id AND d.user_id=a.user_id "
                "WHERE a.guild_id=?",
                (guild_id,),
            )
            values: dict[int, int] = {}
            self._valuation_source(guild_id, values)
        total = [
            (r["user_id"], r["balance"] + values.get(r["user_id"], 0))
            for r in rows
            if user_ids is None or r["user_id"] in user_ids
        ]
        return sorted(total, key=lambda t: (-t[1], t[0]))

    def wealthy_ids_for(self, guild_id: int) -> set[int]:
        """富豪（金融純資産が WEALTHY_ASSETS 以上で、カタギ）のユーザーID。表示だけの身分で、ゲームの条件には使わない。"""
        return {
            uid
            for uid, total in self._ranked(guild_id, None)
            if total >= teiai.WEALTHY_ASSETS
            and self._status_reader.status_for(guild_id, uid, self._now()) == teiai.KATAGI
        }

    def wealthy_ids(self) -> set[int]:
        return self.wealthy_ids_for(self._g())

    def display_status(self, user_id: int, wealthy: set[int] | None = None) -> str:
        """表示用の身分。status() と同じで、カタギで金融純資産が WEALTHY_ASSETS 以上なら WEALTHY。wealthy を渡すと再計算しない。"""
        gid = self._g()
        status = self._status_reader.status_for(gid, user_id, self._now())
        if status != teiai.KATAGI:
            return status
        ids = self.wealthy_ids_for(gid) if wealthy is None else wealthy
        return teiai.WEALTHY if user_id in ids else teiai.KATAGI

    def top(self, limit: int = 10, user_ids: set[int] | None = None) -> list[RankEntry]:
        """[(順位, ユーザーID, 金融純資産), ...]。このサーバーの口座だけで並べる。user_ids を渡すと、さらにその人たちに絞る。"""
        return [(i + 1, uid, bal) for i, (uid, bal) in enumerate(self._ranked(self._g(), user_ids)[:limit])]

    def rank_of(self, user_id: int, user_ids: set[int] | None = None) -> RankEntry | None:
        """(順位, 金融純資産, 対象人数)。登録がなければ None。"""
        ranked = self._ranked(self._g(), user_ids)
        for i, (uid, bal) in enumerate(ranked):
            if uid == user_id:
                return i + 1, bal, len(ranked)
        return None

    def peek_stats(self, user_id: int) -> UserRow | None:
        """読み取り専用。未登録の人を作らずに、成績を返す（未登録なら None）。"""
        with self._lock:
            return self._one(_SELECT_USER_ROW, (self._g(), user_id))

    def stats(self, user_id: int) -> UserRow:
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            self._debt.accrue_debt_interest_for(
                gid, user_id, self._now()
            )  # 画面に出す借金に、帝愛の利息（トイチ）を含める
            row = self._one(_SELECT_USER_ROW, (gid, user_id))
            assert row is not None  # 直前に ensure 済み
            return row

    def history(self, user_id: int, limit: int = 10) -> list[LedgerEntry]:
        with self._lock:
            return self._all(
                "SELECT delta, balance_after, reason, ts, kind, counterparty_id FROM ledger "
                "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
                (self._g(), user_id, limit),
            )

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
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, sender)
            self._account.ensure(gid, receiver)
            sent_today = -(
                self._scalar(
                    "SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE guild_id=? AND user_id=? AND kind='transfer_out' AND ts>=?",
                    (gid, sender, day_start_ts),
                )
            )
            played = self._scalar(
                "SELECT games_played FROM user_play_stats WHERE guild_id=? AND user_id=?", (gid, sender)
            )
            if (played if played is not None else 0) < min_games:
                return TransferResult(False, fail("transfer_min_games", min_games=min_games))
            if sent_today + amount > daily_limit:
                return TransferResult(
                    False, fail("transfer_daily_limit", daily_limit=daily_limit, sent_today=sent_today)
                )
            sender_debt = self._account.debt(gid, sender)
            if sender_debt > 0:
                return TransferResult(False, fail("debt_blocks", debt=sender_debt, verb="送金はできません"))
            if self._account.raw_balance(gid, sender) < amount + fee:
                return TransferResult(False, fail("transfer_insufficient_balance", amount=amount, fee=fee))
            self._account.apply(
                gid, sender, -amount, f"送金（送）→{receiver}", kind="transfer_out", counterparty_id=receiver
            )  # 手数料は別の行にする（1日の上限は、送った額だけで数えるため）
            if fee:
                self._account.apply(gid, sender, -fee, "送金手数料", kind="transfer_fee", counterparty_id=receiver)
            self._account.apply(
                gid, receiver, amount, f"送金（受）←{sender}", income=amount, kind="transfer_in", counterparty_id=sender
            )
            return TransferResult(True, received=amount, fee=fee, sender_balance=self._account.raw_balance(gid, sender))
