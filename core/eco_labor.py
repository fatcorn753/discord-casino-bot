"""地下作業（Economy に混ぜ込む部品）。クリッカー。数字と計算は core/labor.py、表は core/schema.py の labor。

稼ぎは、まず「受け取る前の稼ぎ（pot）」に貯まる。受け取りを押すと、まとめて残高に移る（収入なので、借金があれば天引き）。
上限や、手元の制限はない。day_earned は、画面に出す「今日の稼ぎ」。
"""

import random
import time
from typing import Optional

from core import labor as labor_mod


class LaborMixin:
    def _labor_row(self, user_id: int, now: float, day: str, rng=None) -> dict:
        """行を読む（無ければ作る）。日が変わっていたら、今日の稼ぎを 0 に戻し、疲労は時間で回復させる。呼び出し側のトランザクション内で使う。"""
        self._conn.execute("INSERT OR IGNORE INTO labor (guild_id, user_id) VALUES (?, ?)", (self._g(), user_id))
        row = dict(
            self._conn.execute("SELECT * FROM labor WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
        )
        row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now)
        row["fatigue_at"] = now
        if row["day"] != day:
            row["day"], row["day_earned"] = day, 0
        self._idle_settle(row, now, rng)
        return row

    @staticmethod
    def _counts(row: dict) -> dict:
        return {k: row[k] for k in labor_mod.WORKERS}

    def _idle_settle(self, row: dict, now: float, rng=None) -> None:
        """作業員が、最後に数えてから稼いだ分を、受け取る前の稼ぎ（pot）に足す（STOCK_HOURS まで）。低賃金なら、辞める人も出る。"""
        counts = self._counts(row)
        hours = labor_mod.idle_hours(row["idle_at"], now) if row["idle_at"] > 0 else 0.0
        if sum(counts.values()) > 0 and hours > 0:
            gross = int(labor_mod.idle_rate(counts, row["wage"]) * hours)
            row["pot"] += gross
            row["day_earned"] += gross
            row["earned_total"] += gross
            if row["wage"] == "low":
                rng = rng or random.SystemRandom()
                p = labor_mod.QUIT_PER_DAY * hours / 24.0
                for k in labor_mod.WORKERS:
                    row[k] -= sum(1 for _ in range(counts[k]) if rng.random() < p)
        row["idle_at"] = now

    def _labor_save(self, user_id: int, row: dict) -> None:
        self._conn.execute(
            "UPDATE labor SET pick=?, glove=?, fatigue=?, fatigue_at=?, pot=?, day=?, day_earned=?, "
            "last_click=?, clicks=?, earned_total=?, apprentice=?, miner=?, craftsman=?, foreman=?, dorm=?, "
            "wage=?, idle_at=? WHERE guild_id=? AND user_id=?",
            (
                row["pick"],
                row["glove"],
                row["fatigue"],
                row["fatigue_at"],
                row["pot"],
                row["day"],
                row["day_earned"],
                row["last_click"],
                row["clicks"],
                row["earned_total"],
                row["apprentice"],
                row["miner"],
                row["craftsman"],
                row["foreman"],
                row["dorm"],
                row["wage"],
                row["idle_at"],
                self._g(),
                user_id,
            ),
        )

    def _labor_flush(self, user_id: int, row: dict) -> int:
        """受け取る前の稼ぎを、残高に移す（収入）。残高に入った額（天引きの前）を返す。"""
        amount = row["pot"]
        if amount <= 0:
            return 0
        self._ensure(user_id)
        self._apply(user_id, amount, "地下作業:稼ぎ", income=amount)
        row["pot"] = 0
        return amount

    def labor_state(self, user_id: int, now: Optional[float] = None, day: str = "") -> dict:
        """いまの状態（画面用）。口座や行は、まだ無ければ作らない（読むだけ）。"""
        now = time.time() if now is None else now
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM labor WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            row = (
                dict(r)
                if r
                else {
                    k: 0
                    for k in (
                        ("pick", "glove", "pot", "day_earned", "clicks", "earned_total", "dorm", "idle_at")
                        + labor_mod.WORKERS
                    )
                }
                | {
                    "fatigue": 0.0,
                    "fatigue_at": now,
                    "last_click": 0.0,
                    "day": day,
                    "wage": "normal",
                }
            )
            row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now)
            if row["day"] != day:
                row["day_earned"] = 0
            bal = self._conn.execute(
                "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            row["balance"] = bal["balance"] if bal else 0
            counts = self._counts(row)
            row["workers"] = sum(counts.values())
            row["slots"] = labor_mod.slots(row["dorm"])
            row["rate"] = labor_mod.idle_rate(counts, row["wage"])
            hours = labor_mod.idle_hours(row["idle_at"], now) if row["idle_at"] > 0 and row["workers"] else 0.0
            row["idle_pending"] = int(row["rate"] * hours)  # まだ数えていない、作業員の稼ぎ（見込み）
            row["pot_total"] = row["pot"] + row["idle_pending"]
            return row

    def labor_top(self, limit: int = 10, day: Optional[str] = None, user_id: Optional[int] = None) -> dict:
        """採掘ランキング。day を渡すと、その日の稼ぎ。渡さなければ、通算の稼ぎ。{rows, me}。

        rows は [(順位, ユーザーID, 稼ぎ, 押した回数)]、me は user_id の (順位, 稼ぎ, 押した回数, 人数)（まだ稼いでいなければ None）。
        同額のときは、押した回数が少ない（効率のよい）人が上、それでも同じならユーザーID順。
        """
        col = "day_earned" if day else "earned_total"
        where = "AND day=?" if day else ""
        args = (self._g(), day) if day else (self._g(),)
        with self._lock:
            ranked = self._conn.execute(
                f"SELECT user_id, {col} AS earned, clicks FROM labor WHERE guild_id=? {where} AND {col}>0 "
                f"ORDER BY {col} DESC, clicks ASC, user_id ASC",
                args,
            ).fetchall()
        rows = [(i + 1, r["user_id"], r["earned"], r["clicks"]) for i, r in enumerate(ranked[:limit])]
        me = next(
            ((i + 1, r["earned"], r["clicks"], len(ranked)) for i, r in enumerate(ranked) if r["user_id"] == user_id),
            None,
        )
        return {"rows": rows, "me": me}

    def labor_click(self, user_id: int, now: Optional[float] = None, day: str = "") -> dict:
        """「働く」を1回押す。{ok, reason, pay}。ok=False のときは、何も変わらない。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._labor_row(user_id, now, day)
            if now - row["last_click"] < labor_mod.CLICK_INTERVAL:
                return {"ok": False, "reason": "押すのが早すぎます。", "quiet": True}
            out = labor_mod.click_outcome(row["pick"], row["glove"], row["fatigue"])
            pay = out["pay"]
            row.update(
                fatigue=out["fatigue"],
                last_click=now,
                clicks=row["clicks"] + 1,
                pot=row["pot"] + pay,
                day_earned=row["day_earned"] + pay,
                earned_total=row["earned_total"] + pay,
            )
            self._labor_save(user_id, row)
            return {"ok": True, "pay": pay}

    def labor_collect(self, user_id: int, now: Optional[float] = None, day: str = "") -> int:
        """受け取る前の稼ぎを、残高に移す。移した額（天引きの前）を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self._labor_row(user_id, now, day)
            amount = self._labor_flush(user_id, row)
            self._labor_save(user_id, row)
            return amount

    def labor_upgrade(self, user_id: int, tool: str, now: Optional[float] = None, day: str = "") -> tuple:
        """工具を1段強化する（ペリカを払う）。(ok, 理由) を返す。"""
        if tool not in labor_mod.TOOLS:
            return False, "そんな工具はありません。"
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._labor_row(user_id, now, day)
            cost = labor_mod.upgrade_cost(row[tool])
            if cost is None:
                return False, f"{labor_mod.TOOL_NAME[tool]}は、もう最大です。"
            if self._balance(user_id) < cost:
                return False, f"ペリカが足りません（{cost:,} 必要）。"
            self._apply(user_id, -cost, f"地下作業:{labor_mod.TOOL_NAME[tool]}の強化")
            row[tool] += 1
            self._labor_save(user_id, row)
            return True, ""

    # ---------------------------------------------------------------- #
    # 雇用                                                              #
    # ---------------------------------------------------------------- #
    def labor_hire(self, user_id: int, kind: str, now: Optional[float] = None, day: str = "") -> tuple:
        """作業員を1人雇う（ペリカを払う）。(ok, 理由) を返す。"""
        if kind not in labor_mod.WORKERS:
            return False, "そんな作業員はいません。"
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._labor_row(user_id, now, day)  # 先に、いままでの稼ぎを数える
            if sum(self._counts(row).values()) >= labor_mod.slots(row["dorm"]):
                self._labor_save(user_id, row)
                return False, "人数がいっぱいです（寮を強化するか、誰かを解雇してください）。"
            cost = labor_mod.worker_cost(kind, row[kind])
            if self._balance(user_id) < cost:
                self._labor_save(user_id, row)
                return False, f"ペリカが足りません（{cost:,} 必要）。"
            self._apply(user_id, -cost, f"地下作業:{labor_mod.WORKER_NAME[kind]}を雇う")
            row[kind] += 1
            self._labor_save(user_id, row)
            return True, ""

    def labor_dismiss(self, user_id: int, kind: str, now: Optional[float] = None, day: str = "") -> tuple:
        """作業員を1人解雇する（お金は戻らない）。(ok, 理由) を返す。"""
        if kind not in labor_mod.WORKERS:
            return False, "そんな作業員はいません。"
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self._labor_row(user_id, now, day)
            if row[kind] <= 0:
                self._labor_save(user_id, row)
                return False, f"{labor_mod.WORKER_NAME[kind]}は、いません。"
            row[kind] -= 1
            self._labor_save(user_id, row)
            return True, ""

    def labor_dorm(self, user_id: int, now: Optional[float] = None, day: str = "") -> tuple:
        """寮を1段強化する（雇える人数が +1）。(ok, 理由) を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._labor_row(user_id, now, day)
            cost = labor_mod.dorm_cost(row["dorm"])
            if cost is None:
                self._labor_save(user_id, row)
                return False, "寮は、もう最大です。"
            if self._balance(user_id) < cost:
                self._labor_save(user_id, row)
                return False, f"ペリカが足りません（{cost:,} 必要）。"
            self._apply(user_id, -cost, "地下作業:寮の強化")
            row["dorm"] += 1
            self._labor_save(user_id, row)
            return True, ""

    def labor_set_wage(self, user_id: int, wage: str, now: Optional[float] = None, day: str = "") -> tuple:
        """賃金の設定を変える（変える前の稼ぎは、前の設定で数える）。(ok, 理由) を返す。"""
        if wage not in labor_mod.WAGES:
            return False, "そんな賃金はありません。"
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._labor_row(user_id, now, day)
            row["wage"] = wage
            self._labor_save(user_id, row)
            return True, ""
