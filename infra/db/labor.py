"""地下作業（クリッカー）。数字と計算は domain/labor.py、表は infra/db/schema/ddl/labor.py。

稼ぎは、まず「受け取る前の稼ぎ（pot）」に貯まる。受け取りを押すと、まとめて残高に移る（収入なので、借金があれば天引き）。
「働く」クリックには、1日に押せる回数の上限がある（MAX_CLICKS_PER_DAY。day_click で数える。超えたら1回 OVER_CAP_PAY）。day_earned は、画面に出す「今日の稼ぎ」。
"""

import random
from typing import Any, Literal, cast, overload

from domain import labor as labor_mod
from domain import relics as relics_mod
from domain import shop as shop_mod
from domain import teiai
from domain.errors import Outcome, fail
from domain.ports.bridges import IBankCashFlow
from domain.ports.contracts import (
    LaborClick,
    LaborClickFail,
    LaborClickHourRow,
    LaborClickOk,
    LaborCollectDetail,
    LaborFindSummary,
    LaborState,
    LaborTop,
)
from domain.ports.repos import ILaborRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.inventory import InventoryCore
from infra.db.relics import RelicRepo


def _set_counts(row: dict[str, Any], workers: list[dict[str, Any]]) -> None:
    for k in labor_mod.WORKERS:
        row[k] = sum(1 for w in workers if w["kind"] == k)


def _counts(row: dict[str, Any]) -> dict[str, int]:
    return {k: row[k] for k in labor_mod.WORKERS}


class _NoRoll(random.Random):
    """見込みの表示では、鉱石の抽選をしない（random() が 1.0 なら、何も出ない）。"""

    def random(self) -> float:
        return 1.0


_NO_ROLL = _NoRoll()


class LaborRepo(ScopedRepo, ILaborRepo):
    """地下作業の稼ぎ・契約・鉱石・発掘品。"""

    FINDS_KEEP = 40  # 1人ぶん残す、掘り出しの履歴の数
    CLICK_LOG_DAYS = 14  # 押した記録（1時間ごと）を残す日数

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        relics: RelicRepo,
        bank: IBankCashFlow,
        guild_id: int = 0,
        strict: bool = False,
        *,
        inventory: InventoryCore | None = None,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._relics = relics
        self._bank = bank
        self._inventory = inventory or InventoryCore(db)

    def _consume_drink(self, guild_id: int, user_id: int) -> bool:
        return self._inventory.consume(guild_id, user_id, shop_mod.LABOR_DRINK, 1)

    def _load_labor_row(self, guild_id: int, user_id: int, day: str) -> tuple[dict[str, Any], bool, bool]:
        upgrades = self._one(
            "SELECT pick, glove, dorm, renew FROM labor_upgrades WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        fatigue = self._one(
            "SELECT fatigue, fatigue_at, last_click FROM labor_fatigue WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        pot = self._one(
            "SELECT pot, pot_workers, idle_at FROM labor_pot WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        )
        daily = self._one(
            "SELECT earned, clicks FROM labor_daily WHERE guild_id=? AND user_id=? AND day=?",
            (guild_id, user_id, day),
        )
        totals = self._one(
            "SELECT clicks, earned_total, donated FROM labor_totals WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        ores = {
            r["ore"]: r["qty"]
            for r in self._all(
                "SELECT ore, qty FROM labor_pending_ores WHERE guild_id=? AND user_id=? AND qty>0",
                (guild_id, user_id),
            )
            if r["ore"] in labor_mod.ORE_VALUE
        }
        relics = {
            r["item_id"]: r["qty"]
            for r in self._all(
                "SELECT item_id, qty FROM labor_pending_relics WHERE guild_id=? AND user_id=? AND qty>0",
                (guild_id, user_id),
            )
            if r["item_id"] in shop_mod.BY_ID
        }
        row: dict[str, Any] = {
            "pick": upgrades["pick"] if upgrades else 0,
            "glove": upgrades["glove"] if upgrades else 0,
            "dorm": upgrades["dorm"] if upgrades else 0,
            "renew": upgrades["renew"] if upgrades else 1,
            "fatigue": fatigue["fatigue"] if fatigue else 0.0,
            "fatigue_at": fatigue["fatigue_at"] if fatigue else 0.0,
            "last_click": fatigue["last_click"] if fatigue else 0.0,
            "pot": pot["pot"] if pot else 0,
            "pot_workers": pot["pot_workers"] if pot else 0,
            "idle_at": pot["idle_at"] if pot else 0.0,
            "day": day,
            "day_earned": daily["earned"] if daily else 0,
            "day_click": daily["clicks"] if daily else 0,
            "clicks": totals["clicks"] if totals else 0,
            "earned_total": totals["earned_total"] if totals else 0,
            "donated": totals["donated"] if totals else 0,
            "ores": ores,
            "relics": relics,
        }
        has_any = any((upgrades, fatigue, pot, daily, totals, ores, relics))
        return row, has_any, upgrades is not None

    def _labor_row(
        self, guild_id: int, user_id: int, now: float, day: str, rng: random.Random | None = None
    ) -> dict[str, Any]:
        """行を読む（無ければ既定値で組み立てる）。疲労は時間で回復させ、作業員の稼ぎと契約も決算する。呼び出し側のトランザクション内で使う。"""
        row, has_any, has_upgrades = self._load_labor_row(guild_id, user_id, day)
        row["_existed"] = has_any
        row["_has_upgrades"] = has_upgrades
        buffs = self._relics.relic_buffs_for(guild_id, user_id)
        row["_buffs"] = buffs
        row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now, buffs.get("recovery", 0.0))
        row["fatigue_at"] = now
        self._idle_settle(guild_id, user_id, row, now, buffs, rng)
        return row

    def labor_donated_total(self) -> int:
        """このサーバーの全員の、慈善団体への寄付（帝愛のピンハネ）の合計。"""
        with self._lock:
            return self._scalar("SELECT COALESCE(SUM(donated), 0) FROM labor_totals WHERE guild_id=?", (self._g(),))

    def _labor_log_finds(self, guild_id: int, user_id: int, finds: list[Any]) -> None:
        """掘り出しの履歴を書く（古いものは、FINDS_KEEP 件まで残して消す）。"""
        if not finds:
            return
        self._conn.executemany(
            "INSERT INTO labor_finds (guild_id, user_id, at, kind, ore, qty) VALUES (?, ?, ?, ?, ?, ?)",
            [(guild_id, user_id, at, kind, ore, n) for at, kind, ore, n in finds],
        )
        self._conn.execute(
            "DELETE FROM labor_finds WHERE guild_id=? AND user_id=? AND id NOT IN "
            "(SELECT id FROM labor_finds WHERE guild_id=? AND user_id=? ORDER BY at DESC, id DESC LIMIT ?)",
            (guild_id, user_id, guild_id, user_id, self.FINDS_KEEP),
        )

    def labor_finds(self, user_id: int, limit: int = 6) -> list[LaborFindSummary]:
        """最近の掘り出し（石炭は除く）。すでにペリカ等を受け取り済みのものは出さない。新しい順。[{at, kind, ore}]。"""
        with self._lock:
            return self._all(
                "SELECT at, kind, ore FROM labor_finds WHERE guild_id=? AND user_id=? AND collected=0 AND ore NOT IN ({}) "
                "ORDER BY at DESC, id DESC LIMIT ?".format(",".join("?" * len(labor_mod.FLAT))),
                (self._g(), user_id, *labor_mod.FLAT, limit),
            )

    def _workers_of(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, kind, expires_at FROM labor_workers WHERE guild_id=? AND user_id=? ORDER BY expires_at, id",
            (guild_id, user_id),
        )

    def _idle_settle(
        self,
        guild_id: int,
        user_id: int,
        row: dict[str, Any],
        now: float,
        buffs: dict[str, float],
        rng: random.Random | None = None,
    ) -> None:
        """作業員の稼ぎを pot に足し、切れた契約を決算する（更新なら契約料を払い、払えなければ退職）。"""
        workers = self._workers_of(guild_id, user_id)
        if workers:
            bal = self._one("SELECT balance FROM accounts WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            plan = labor_mod.settle_plan(
                workers,
                bool(row["renew"]),
                row["idle_at"],
                now,
                row["pot"],
                bal["balance"] if bal else 0,
                rng,
                wage_bonus=buffs.get("wage", 0.0),
                luck_bonus=buffs.get("luck", 0.0),
            )
            if plan["balance_spent"] > 0:
                self._account.apply(guild_id, user_id, -plan["balance_spent"], "地下作業:契約の更新")
            for wid, expires in plan["renewed"]:
                self._conn.execute("UPDATE labor_workers SET expires_at=? WHERE id=?", (expires, wid))
            for wid in plan["removed"]:
                self._conn.execute("DELETE FROM labor_workers WHERE id=?", (wid,))
            row["pot"] += plan["gain"] - plan["pot_spent"]
            row["pot_workers"] = min(row["pot"], max(0, row["pot_workers"] + plan["gain"] - plan["pot_spent"]))
            for k, n in plan["relics"].items():
                if k in shop_mod.BY_ID and n > 0:
                    row["relics"][k] = row["relics"].get(k, 0) + n
            for k, n in plan["ores"].items():
                if k in labor_mod.ORE_VALUE and n > 0:
                    row["ores"][k] = row["ores"].get(k, 0) + n
            self._labor_log_finds(guild_id, user_id, plan["finds"])
            row["day_earned"] += plan["gain"]
            row["earned_total"] += plan["gain"]
            workers = self._workers_of(guild_id, user_id)
        _set_counts(row, workers)
        row["idle_at"] = now

    def _sync_pending_map(self, table: str, key_col: str, guild_id: int, user_id: int, counts: dict[str, int]) -> None:
        self._conn.execute(f"DELETE FROM {table} WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        items = [(guild_id, user_id, k, int(v)) for k, v in counts.items() if int(v) > 0]
        if items:
            self._conn.executemany(
                f"INSERT INTO {table} (guild_id, user_id, {key_col}, qty) VALUES (?, ?, ?, ?)",
                items,
            )

    def _labor_save(self, guild_id: int, user_id: int, row: dict[str, Any]) -> None:
        if row.get("_has_upgrades") or row["pick"] > 0 or row["glove"] > 0 or row["dorm"] > 0 or row["renew"] != 1:
            self._conn.execute(
                "INSERT INTO labor_upgrades (guild_id, user_id, pick, glove, dorm, renew) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "pick=excluded.pick, glove=excluded.glove, dorm=excluded.dorm, renew=excluded.renew",
                (guild_id, user_id, row["pick"], row["glove"], row["dorm"], row["renew"]),
            )
        if row["fatigue"] > 0 or row["fatigue_at"] > 0 or row["last_click"] > 0:
            self._conn.execute(
                "INSERT INTO labor_fatigue (guild_id, user_id, fatigue, fatigue_at, last_click) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "fatigue=excluded.fatigue, fatigue_at=excluded.fatigue_at, last_click=excluded.last_click",
                (guild_id, user_id, row["fatigue"], row["fatigue_at"], row["last_click"]),
            )
        if row["pot"] > 0 or row["pot_workers"] > 0 or row["idle_at"] > 0:
            self._conn.execute(
                "INSERT INTO labor_pot (guild_id, user_id, pot, pot_workers, idle_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "pot=excluded.pot, pot_workers=excluded.pot_workers, idle_at=excluded.idle_at",
                (guild_id, user_id, row["pot"], row["pot_workers"], row["idle_at"]),
            )
        if row["day_earned"] > 0 or row["day_click"] > 0:
            self._conn.execute(
                "INSERT INTO labor_daily (guild_id, user_id, day, earned, clicks) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id, day) DO UPDATE SET earned=excluded.earned, clicks=excluded.clicks",
                (guild_id, user_id, row["day"], row["day_earned"], row["day_click"]),
            )
        if row["clicks"] > 0 or row["earned_total"] > 0 or row["donated"] > 0:
            self._conn.execute(
                "INSERT INTO labor_totals (guild_id, user_id, clicks, earned_total, donated) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "clicks=excluded.clicks, earned_total=excluded.earned_total, donated=excluded.donated",
                (guild_id, user_id, row["clicks"], row["earned_total"], row["donated"]),
            )
        self._sync_pending_map("labor_pending_ores", "ore", guild_id, user_id, row["ores"])
        self._sync_pending_map("labor_pending_relics", "item_id", guild_id, user_id, row["relics"])

    def _labor_flush(self, guild_id: int, user_id: int, row: dict[str, Any]) -> int:
        """受け取る前の稼ぎを、残高に移す（収入）。残高に入った額（天引きの前）を返す。"""
        amount = row["pot"]
        if amount <= 0:
            return 0
        self._account.ensure(guild_id, user_id)
        skimmed = teiai.labor_skim(amount)  # 地下労働なので、帝愛がピンハネする（借金の有無に関わらず）
        net = amount - skimmed
        if skimmed:
            self._account.ledger(
                guild_id,
                user_id,
                0,
                self._account.raw_balance(guild_id, user_id),
                f"慈善団体への寄付（地下作業のピンハネ {skimmed:,}ペリカ）",
            )
            self._bank.bank_income_for(guild_id, skimmed, self._now())  # 帝愛銀行へ入る（サーモスタット経由）
        self._account.apply(guild_id, user_id, net, "地下作業:稼ぎ", income=net)
        row["pot"] = 0
        row["pot_workers"] = 0
        row["ores"] = {}
        row["skimmed"] = skimmed
        row["donated"] = row.get("donated", 0) + skimmed
        return net

    def labor_state(self, user_id: int, now: float, day: str = "") -> LaborState:
        """いまの状態（画面用）。口座や行は、まだ無ければ作らない（読むだけ）。契約の決算は、したことにして見せる（書き込まない）。"""
        with self._lock:
            gid = self._g()
            row, _, _ = self._load_labor_row(gid, user_id, day)
            buffs = self._relics.relic_buffs_for(gid, user_id)
            row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now, buffs.get("recovery", 0.0))
            bal = self._scalar("SELECT balance FROM accounts WHERE guild_id=? AND user_id=?", (gid, user_id))
            row["balance"] = bal if bal is not None else 0
            workers = self._workers_of(gid, user_id)
            plan = labor_mod.settle_plan(
                workers,
                bool(row["renew"]),
                row["idle_at"],
                now,
                row["pot"],
                row["balance"],
                _NO_ROLL,
                wage_bonus=buffs.get("wage", 0.0),
                luck_bonus=buffs.get("luck", 0.0),
            )
            gone = set(plan["removed"])
            renewed = dict(plan["renewed"])
            stay = [{**w, "expires_at": renewed.get(w["id"], w["expires_at"])} for w in workers if w["id"] not in gone]
            _set_counts(row, stay)
            row["workers"] = len(stay)
            row["slots"] = labor_mod.slots(row["dorm"])
            row["rate"] = labor_mod.idle_rate(_counts(row))
            row["idle_pending"] = plan["gain"]  # まだ数えていない、作業員の稼ぎ（見込み）
            row["pot_total"] = row["pot"] + plan["gain"] - plan["pot_spent"]
            row["pot_workers_total"] = min(
                row["pot_total"], max(0, row["pot_workers"] + plan["gain"] - plan["pot_spent"])
            )  # 未受け取りのうち、作業員の分（まだ数えていない見込みも含む）
            row["renew_fee"] = plan["pot_spent"] + plan["balance_spent"]  # 切れていた契約の更新に、いま払う額
            row["expires"] = {}
            for w in stay:
                row["expires"][w["kind"]] = min(row["expires"].get(w["kind"], w["expires_at"]), w["expires_at"])
            row["drinks"] = self._inventory.qty(gid, user_id, shop_mod.LABOR_DRINK)
            row["finds"] = self.labor_finds(user_id)
            row["held_relics"] = self._relics.relic_stock_for(gid, user_id)
            row["donated_all"] = self.labor_donated_total()  # このサーバーの全員の寄付の合計
            row["terrain"] = labor_mod.terrain_today(day)  # 今日の地層
            row["crit_chance"] = labor_mod.crit_chance(row["pick"], row["workers"], row["terrain"])
            return cast("LaborState", row)

    def labor_top(self, limit: int = 10, day: str | None = None, user_id: int | None = None) -> LaborTop:
        """採掘ランキング。day を渡すと、その日の稼ぎ。渡さなければ、通算の稼ぎ。{rows, me}。"""
        with self._lock:
            gid = self._g()
            if day is not None:
                ranked = self._all(
                    "SELECT d.user_id, d.earned, COALESCE(t.clicks, 0) AS clicks "
                    "FROM labor_daily d "
                    "LEFT JOIN labor_totals t ON t.guild_id=d.guild_id AND t.user_id=d.user_id "
                    "WHERE d.guild_id=? AND d.day=? AND d.earned>0 "
                    "ORDER BY d.earned DESC, clicks ASC, d.user_id ASC",
                    (gid, day),
                )
            else:
                ranked = self._all(
                    "SELECT user_id, earned_total AS earned, clicks "
                    "FROM labor_totals WHERE guild_id=? AND earned_total>0 "
                    "ORDER BY earned_total DESC, clicks ASC, user_id ASC",
                    (gid,),
                )
        rows = [(i + 1, r["user_id"], r["earned"], r["clicks"]) for i, r in enumerate(ranked[:limit])]
        me = next(
            ((i + 1, r["earned"], r["clicks"], len(ranked)) for i, r in enumerate(ranked) if r["user_id"] == user_id),
            None,
        )
        result: LaborTop = {"rows": rows, "me": me}
        return result

    def labor_click(self, user_id: int, now: float, day: str = "", rng: random.Random | None = None) -> LaborClick:
        """「働く」を1回押す。{ok, reason, pay, crit, ore, combo, terrain}。"""
        rng = rng or cast("random.Random", random)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            row = self._labor_row(gid, user_id, now, day)
            if now - row["last_click"] < labor_mod.CLICK_INTERVAL:
                early: LaborClickFail = {"ok": False, "reason": "押すのが早すぎます。", "quiet": True}
                return early
            terrain = labor_mod.terrain_today(day)
            gain = labor_mod.fatigue_gain(row["glove"])
            if terrain == "fatigue":
                gain *= 1 - labor_mod.TERRAIN_FATIGUE_CUT  # 今日の地層: 疲労の溜まりが軽い
            out = {
                "pay": labor_mod.click_pay(row["pick"], row["fatigue"]),
                "fatigue": min(labor_mod.FATIGUE_MAX, row["fatigue"] + gain),
            }
            pay = max(1, relics_mod.chance_round(out["pay"] * (1 + row["_buffs"].get("click", 0.0))))
            room = labor_mod.MAX_CLICKS_PER_DAY - row["day_click"]
            hour = int(now // 3600)
            since = now - labor_mod.SUSPECT_HOURS * 3600
            suspected = labor_mod.auto_click_suspected(self.labor_click_hours(user_id, since), hour)
            capped = room <= 0 or suspected
            crit, ore, combo = False, None, False
            if not capped:
                workers = len(self._workers_of(gid, user_id))
                crit = rng.random() < labor_mod.crit_chance(row["pick"], workers, terrain)
                if crit:
                    pay *= labor_mod.CRIT_MULT
                if crit and rng.random() < labor_mod.CRIT_ORE_CHANCE:
                    ore = labor_mod.roll_crit_ore(rng, terrain)
                    pay += labor_mod.ORE_VALUE[ore]
                combo = (row["clicks"] + 1) % labor_mod.COMBO_SIZE == 0
                if combo:
                    pay = round(pay * (1 + labor_mod.COMBO_BONUS))
            pay = labor_mod.OVER_CAP_PAY if capped else pay
            if capped:
                crit, ore, combo = False, None, False
            gap = now - row["last_click"]
            row.update(
                fatigue=out["fatigue"],
                last_click=now,
                clicks=row["clicks"] + 1,
                pot=row["pot"] + pay,
                day_earned=row["day_earned"] + pay,
                day_click=row["day_click"] + 1,
                earned_total=row["earned_total"] + pay,
            )
            if ore:
                row["ores"][ore] = row["ores"].get(ore, 0) + 1
            self._labor_log_click(gid, user_id, now, pay, gap)
            self._labor_save(gid, user_id, row)
            clicked: LaborClickOk = {
                "ok": True,
                "pay": pay,
                "crit": crit,
                "ore": ore,
                "combo": combo,
                "terrain": terrain,
            }
            return clicked

    def _labor_log_click(self, guild_id: int, user_id: int, now: float, pay: int, gap: float) -> None:
        """「働く」の1回を、1時間ごとの記録に足す（押した回数・稼ぎ・間隔の集計）。"""
        hour = int(now // 3600)
        near = 0.0 < gap <= labor_mod.CLICK_GAP_MAX
        self._conn.execute(
            "INSERT INTO labor_click_hours (guild_id, user_id, hour, clicks, pay, gaps, sum_gap, sum_gap2, min_gap) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id, hour) DO UPDATE SET "
            "clicks=clicks+1, pay=pay+excluded.pay, gaps=gaps+excluded.gaps, sum_gap=sum_gap+excluded.sum_gap, "
            "sum_gap2=sum_gap2+excluded.sum_gap2, min_gap=CASE WHEN excluded.min_gap IS NULL THEN min_gap "
            "WHEN min_gap IS NULL OR excluded.min_gap<min_gap THEN excluded.min_gap ELSE min_gap END",
            (
                guild_id,
                user_id,
                hour,
                pay,
                1 if near else 0,
                gap if near else 0.0,
                gap * gap if near else 0.0,
                gap if near else None,
            ),
        )
        if hour % 24 == 0:  # 古い記録は、1日1回ほど掃除する
            self._conn.execute("DELETE FROM labor_click_hours WHERE hour<?", (hour - self.CLICK_LOG_DAYS * 24,))

    def labor_click_hours(self, user_id: int, since: float) -> list[LaborClickHourRow]:
        """since 以降の、1時間ごとの押した記録（古い順）。"""
        with self._lock:
            return self._all(
                "SELECT hour, clicks, pay, gaps, sum_gap, sum_gap2, min_gap FROM labor_click_hours "
                "WHERE guild_id=? AND user_id=? AND hour>=? ORDER BY hour",
                (self._g(), user_id, int(since // 3600)),
            )

    @overload
    def labor_collect(self, user_id: int, now: float, day: str = "", detail: Literal[False] = False) -> int: ...
    @overload
    def labor_collect(self, user_id: int, now: float, day: str, detail: Literal[True]) -> LaborCollectDetail: ...
    def labor_collect(self, user_id: int, now: float, day: str = "", detail: bool = False) -> int | LaborCollectDetail:
        """受け取る前の稼ぎから、帝愛のピンハネを引いて、残高に移す。移した額（借金の天引きの前）を返す。"""
        with self._db.tx():
            gid = self._g()
            row = self._labor_row(gid, user_id, now, day)
            ores = dict(row["ores"])
            relics = dict(row["relics"])
            row["skimmed"] = 0
            amount = self._labor_flush(gid, user_id, row)
            self._conn.execute(
                "UPDATE labor_finds SET collected=1 WHERE guild_id=? AND user_id=? AND collected=0",
                (gid, user_id),
            )
            for item_id, n in relics.items():
                self._inventory.add(gid, user_id, item_id, n)
            row["relics"] = {}
            if row["_existed"] or amount > 0 or relics or self._workers_of(gid, user_id):
                self._labor_save(gid, user_id, row)
            if not detail:
                return amount
            detail_result: LaborCollectDetail = {
                "amount": amount,
                "skimmed": row["skimmed"],
                "donated": row["donated"],
                "donated_all": self.labor_donated_total(),
                "ores": ores,
                "relics": relics,
            }
            return detail_result

    def labor_drink(self, user_id: int, now: float, day: str = "") -> Outcome[str]:
        """栄養ドリンコを1本飲んで、疲労を DRINK_RECOVER 回復する。(ok, 理由) を返す。疲れていなければ、減らさない。"""
        with self._db.tx():
            gid = self._g()
            row = self._labor_row(gid, user_id, now, day)
            if row["fatigue"] < 1:
                if row["_existed"] or self._workers_of(gid, user_id):
                    self._labor_save(gid, user_id, row)
                return False, fail("labor_not_tired")
            if not self._consume_drink(gid, user_id):
                if row["_existed"] or self._workers_of(gid, user_id):
                    self._labor_save(gid, user_id, row)
                return False, fail("labor_no_drink")
            row["fatigue"] = max(0.0, row["fatigue"] - labor_mod.DRINK_RECOVER)
            self._labor_save(gid, user_id, row)
            return True, ""

    def labor_upgrade(self, user_id: int, tool: str, now: float, day: str = "") -> Outcome[str]:
        """工具を1段強化する（ペリカを払う）。(ok, Failure | "") を返す。"""
        if tool not in labor_mod.TOOLS:
            return False, fail("labor_unknown_tool")
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            row = self._labor_row(gid, user_id, now, day)
            cost = labor_mod.upgrade_cost(row[tool])
            if cost is None:
                return False, fail("labor_tool_max", tool_name=labor_mod.TOOL_NAME[tool])
            if self._account.raw_balance(gid, user_id) < cost:
                return False, fail("labor_insufficient_balance", cost=cost)
            self._account.apply(gid, user_id, -cost, f"地下作業:{labor_mod.TOOL_NAME[tool]}の強化")
            row[tool] += 1
            self._labor_save(gid, user_id, row)
            return True, ""

    def labor_hire(self, user_id: int, kind: str, now: float, day: str = "") -> Outcome[str]:
        """作業員を1人雇う（契約料を払う。契約は CONTRACT_DAYS 日）。(ok, Failure | "") を返す。"""
        if kind not in labor_mod.WORKERS:
            return False, fail("labor_unknown_worker")
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            row = self._labor_row(gid, user_id, now, day)  # 先に、いままでの稼ぎと契約を決算する
            if sum(_counts(row).values()) >= labor_mod.slots(row["dorm"]):
                self._labor_save(gid, user_id, row)
                return False, fail("labor_slots_full")
            cost = labor_mod.contract_fee(kind)
            if self._account.raw_balance(gid, user_id) < cost:
                self._labor_save(gid, user_id, row)
                return False, fail("labor_hire_insufficient_balance", cost=cost)
            self._account.apply(gid, user_id, -cost, f"地下作業:{labor_mod.WORKER_NAME[kind]}と契約")
            self._conn.execute(
                "INSERT INTO labor_workers (guild_id, user_id, kind, expires_at) VALUES (?, ?, ?, ?)",
                (gid, user_id, kind, now + labor_mod.CONTRACT_DAYS * 86400.0),
            )
            self._labor_save(gid, user_id, row)
            return True, ""

    def labor_dismiss(self, user_id: int, kind: str, now: float, day: str = "") -> Outcome[str]:
        """作業員を1人解雇する（契約料は戻らない）。契約がいちばん早く切れる人から。(ok, Failure | "") を返す。"""
        if kind not in labor_mod.WORKERS:
            return False, fail("labor_unknown_worker")
        with self._db.tx():
            gid = self._g()
            row = self._labor_row(gid, user_id, now, day)
            target = next((w for w in self._workers_of(gid, user_id) if w["kind"] == kind), None)
            if row["_existed"] or target is not None or self._workers_of(gid, user_id):
                self._labor_save(gid, user_id, row)
            if target is None:
                return False, fail("labor_worker_absent", worker_name=labor_mod.WORKER_NAME[kind])
            self._conn.execute("DELETE FROM labor_workers WHERE id=?", (target["id"],))
            return True, ""

    def labor_dorm(self, user_id: int, now: float, day: str = "") -> Outcome[str]:
        """寮を1段強化する（雇える人数が +1）。(ok, Failure | "") を返す。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            row = self._labor_row(gid, user_id, now, day)
            cost = labor_mod.dorm_cost(row["dorm"])
            if cost is None:
                if row["_existed"] or self._workers_of(gid, user_id):
                    self._labor_save(gid, user_id, row)
                return False, fail("labor_dorm_max")
            if self._account.raw_balance(gid, user_id) < cost:
                if row["_existed"] or self._workers_of(gid, user_id):
                    self._labor_save(gid, user_id, row)
                return False, fail("labor_insufficient_balance", cost=cost)
            self._account.apply(gid, user_id, -cost, "地下作業:寮の強化")
            row["dorm"] += 1
            self._labor_save(gid, user_id, row)
            return True, ""

    def labor_set_renew(self, user_id: int, on: bool, now: float, day: str = "") -> Outcome[str]:
        """契約の自動更新を、オン・オフにする。(ok, 理由) を返す。"""
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            row = self._labor_row(gid, user_id, now, day)
            row["renew"] = 1 if on else 0
            self._labor_save(gid, user_id, row)
            return True, ""
