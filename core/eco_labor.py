"""地下作業（Economy に混ぜ込む部品）。クリッカー。数字と計算は core/labor.py、表は core/schema.py の labor。

稼ぎは、まず「受け取る前の稼ぎ（pot）」に貯まる。受け取りを押すと、まとめて残高に移る（収入なので、借金があれば天引き）。
上限や、手元の制限はない。day_earned は、画面に出す「今日の稼ぎ」。
"""

import json
import time
from typing import Optional

from core import labor as labor_mod
from core import shop as shop_mod


class LaborMixin:
    def _labor_row(self, user_id: int, now: float, day: str, rng=None) -> dict:
        """行を読む（無ければ作る）。日が変わっていたら、今日の稼ぎを 0 に戻し、疲労は時間で回復させる。呼び出し側のトランザクション内で使う。

        作業員の稼ぎと契約も、ここで決算する（稼ぎは pot に足し、契約が切れた人は、更新か退職）。
        """
        self._conn.execute("INSERT OR IGNORE INTO labor (guild_id, user_id) VALUES (?, ?)", (self._g(), user_id))
        row = dict(
            self._conn.execute("SELECT * FROM labor WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
        )
        row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now)
        row["fatigue_at"] = now
        if row["day"] != day:
            row["day"], row["day_earned"] = day, 0
        self._idle_settle(user_id, row, now, rng)
        return row

    @staticmethod
    def _counts(row: dict) -> dict:
        return {k: row[k] for k in labor_mod.WORKERS}

    @staticmethod
    def _ores_of(row: dict) -> dict:
        """受け取る前の稼ぎに含まれる鉱石 {鉱石: 個数}。"""
        try:
            return {k: int(n) for k, n in json.loads(row.get("ores") or "{}").items() if k in labor_mod.ORE_VALUE}
        except (ValueError, AttributeError):
            return {}

    def _workers_of(self, user_id: int) -> list:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT id, kind, expires_at FROM labor_workers WHERE guild_id=? AND user_id=? ORDER BY expires_at, id",
                (self._g(), user_id),
            )
        ]

    @staticmethod
    def _set_counts(row: dict, workers: list) -> None:
        for k in labor_mod.WORKERS:
            row[k] = sum(1 for w in workers if w["kind"] == k)

    def _idle_settle(self, user_id: int, row: dict, now: float, rng=None) -> None:
        """作業員の稼ぎを pot に足し、切れた契約を決算する（更新なら契約料を払い、払えなければ退職）。低賃金なら、辞める人も出る。"""
        workers = self._workers_of(user_id)
        if workers:
            bal = self._conn.execute(
                "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            plan = labor_mod.settle_plan(
                workers,
                row["wage"],
                bool(row["renew"]),
                row["idle_at"],
                now,
                row["pot"],
                bal["balance"] if bal else 0,
                rng,
            )
            if plan["balance_spent"] > 0:
                self._apply(user_id, -plan["balance_spent"], "地下作業:契約の更新")
            for wid, expires in plan["renewed"]:
                self._conn.execute("UPDATE labor_workers SET expires_at=? WHERE id=?", (expires, wid))
            for wid in plan["removed"] + plan["quit"]:
                self._conn.execute("DELETE FROM labor_workers WHERE id=?", (wid,))
            row["pot"] += plan["gain"] - plan["pot_spent"]
            if plan["ores"]:
                found = self._ores_of(row)
                for k, n in plan["ores"].items():
                    found[k] = found.get(k, 0) + n
                row["ores"] = json.dumps(found)
            row["day_earned"] += plan["gain"]
            row["earned_total"] += plan["gain"]
            workers = self._workers_of(user_id)
        self._set_counts(row, workers)
        row["idle_at"] = now

    def _labor_save(self, user_id: int, row: dict) -> None:
        self._conn.execute(
            "UPDATE labor SET pick=?, glove=?, fatigue=?, fatigue_at=?, pot=?, day=?, day_earned=?, "
            "last_click=?, clicks=?, earned_total=?, dorm=?, wage=?, idle_at=?, renew=?, ores=? WHERE guild_id=? AND user_id=?",
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
                row["dorm"],
                row["wage"],
                row["idle_at"],
                row["renew"],
                row.get("ores", ""),
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
        row["ores"] = ""
        return amount

    def labor_state(self, user_id: int, now: Optional[float] = None, day: str = "") -> dict:
        """いまの状態（画面用）。口座や行は、まだ無ければ作らない（読むだけ）。契約の決算は、したことにして見せる（書き込まない）。"""
        now = time.time() if now is None else now

        class _NoQuit:  # 見込みの表示では、辞める人は数えない
            @staticmethod
            def random():
                return 1.0

        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM labor WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            row = (
                dict(r)
                if r
                else {k: 0 for k in ("pick", "glove", "pot", "day_earned", "clicks", "earned_total", "dorm", "idle_at")}
                | {
                    "fatigue": 0.0,
                    "fatigue_at": now,
                    "last_click": 0.0,
                    "day": day,
                    "wage": "normal",
                    "renew": 1,
                    "ores": "",
                }
            )
            row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now)
            if row["day"] != day:
                row["day_earned"] = 0
            bal = self._conn.execute(
                "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            row["balance"] = bal["balance"] if bal else 0
            workers = self._workers_of(user_id)
            plan = labor_mod.settle_plan(
                workers,
                row["wage"],
                bool(row["renew"]),
                row["idle_at"],
                now,
                row["pot"],
                row["balance"],
                _NoQuit,
            )
            gone = set(plan["removed"])
            renewed = dict(plan["renewed"])
            stay = [{**w, "expires_at": renewed.get(w["id"], w["expires_at"])} for w in workers if w["id"] not in gone]
            self._set_counts(row, stay)
            row["workers"] = len(stay)
            row["slots"] = labor_mod.slots(row["dorm"])
            row["rate"] = labor_mod.idle_rate(self._counts(row), row["wage"])
            row["ores"] = self._ores_of(row)  # 決算ずみの鉱石（見込みの分は、掘るまで分からない）
            row["idle_pending"] = plan["gain"]  # まだ数えていない、作業員の稼ぎ（見込み）
            row["pot_total"] = row["pot"] + plan["gain"] - plan["pot_spent"]
            row["renew_fee"] = plan["pot_spent"] + plan["balance_spent"]  # 切れていた契約の更新に、いま払う額
            # 種類ごとの、いちばん早く切れる契約の期限（画面用）
            row["expires"] = {}
            for w in stay:
                row["expires"][w["kind"]] = min(row["expires"].get(w["kind"], w["expires_at"]), w["expires_at"])
            inv = self._conn.execute(
                "SELECT qty FROM inventory WHERE guild_id=? AND user_id=? AND item_id=?",
                (self._g(), user_id, shop_mod.LABOR_DRINK),
            ).fetchone()
            row["drinks"] = inv["qty"] if inv else 0
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

    def labor_collect(self, user_id: int, now: Optional[float] = None, day: str = "", detail: bool = False):
        """受け取る前の稼ぎを、残高に移す。移した額（天引きの前）を返す。detail=True なら {amount, ores}（掘った鉱石つき）。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self._labor_row(user_id, now, day)
            ores = self._ores_of(row)
            amount = self._labor_flush(user_id, row)
            self._labor_save(user_id, row)
            return {"amount": amount, "ores": ores} if detail else amount

    def labor_drink(self, user_id: int, now: Optional[float] = None, day: str = "") -> tuple:
        """栄養ドリンクを1本飲んで、疲労を DRINK_RECOVER 回復する。(ok, 理由) を返す。疲れていなければ、減らさない。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self._labor_row(user_id, now, day)
            if row["fatigue"] < 1:
                self._labor_save(user_id, row)
                return False, "疲れていません。"
            if not self._consume(user_id, shop_mod.LABOR_DRINK):
                self._labor_save(user_id, row)
                return False, "栄養ドリンクを持っていません（ショップの消耗品で買えます）。"
            row["fatigue"] = max(0.0, row["fatigue"] - labor_mod.DRINK_RECOVER)
            self._labor_save(user_id, row)
            return True, ""

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
        """作業員を1人雇う（契約料を払う。契約は CONTRACT_DAYS 日）。(ok, 理由) を返す。"""
        if kind not in labor_mod.WORKERS:
            return False, "そんな作業員はいません。"
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._labor_row(user_id, now, day)  # 先に、いままでの稼ぎと契約を決算する
            if sum(self._counts(row).values()) >= labor_mod.slots(row["dorm"]):
                self._labor_save(user_id, row)
                return False, "人数がいっぱいです（寮を強化するか、誰かを解雇してください）。"
            cost = labor_mod.contract_fee(kind)
            if self._balance(user_id) < cost:
                self._labor_save(user_id, row)
                return False, f"ペリカが足りません（契約料 {cost:,} 必要）。"
            self._apply(user_id, -cost, f"地下作業:{labor_mod.WORKER_NAME[kind]}と契約")
            self._conn.execute(
                "INSERT INTO labor_workers (guild_id, user_id, kind, expires_at) VALUES (?, ?, ?, ?)",
                (self._g(), user_id, kind, now + labor_mod.CONTRACT_DAYS * 86400.0),
            )
            self._labor_save(user_id, row)
            return True, ""

    def labor_dismiss(self, user_id: int, kind: str, now: Optional[float] = None, day: str = "") -> tuple:
        """作業員を1人解雇する（契約料は戻らない）。契約がいちばん早く切れる人から。(ok, 理由) を返す。"""
        if kind not in labor_mod.WORKERS:
            return False, "そんな作業員はいません。"
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self._labor_row(user_id, now, day)
            target = next((w for w in self._workers_of(user_id) if w["kind"] == kind), None)
            self._labor_save(user_id, row)
            if target is None:
                return False, f"{labor_mod.WORKER_NAME[kind]}は、いません。"
            self._conn.execute("DELETE FROM labor_workers WHERE id=?", (target["id"],))
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

    def labor_set_renew(self, user_id: int, on: bool, now: Optional[float] = None, day: str = "") -> tuple:
        """契約の自動更新を、オン・オフにする。(ok, 理由) を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._labor_row(user_id, now, day)
            row["renew"] = 1 if on else 0
            self._labor_save(user_id, row)
            return True, ""
