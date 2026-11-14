"""地下作業（Economy に混ぜ込む部品）。クリッカー。数字と計算は core/labor.py、表は core/schema.py の labor。

稼ぎは、まず「受け取る前の稼ぎ（pot）」に貯まる。受け取りを押すと、まとめて残高に移る（収入なので、借金があれば天引き）。
「働く」クリックには、1日に押せる回数の上限がある（MAX_CLICKS_PER_DAY。day_click で数える。超えたら1回 OVER_CAP_PAY）。day_earned は、画面に出す「今日の稼ぎ」。
"""

import json
import time
from typing import Optional

from core import labor as labor_mod
from core import relics as relics_mod
from core import shop as shop_mod
from core import teiai


class LaborMixin:
    def _labor_row(self, user_id: int, now: float, day: str, rng=None) -> dict:
        """行を読む（無ければ作る）。日が変わっていたら、今日の稼ぎを 0 に戻し、疲労は時間で回復させる。呼び出し側のトランザクション内で使う。

        作業員の稼ぎと契約も、ここで決算する（稼ぎは pot に足し、契約が切れた人は、更新か退職）。
        """
        self._conn.execute("INSERT OR IGNORE INTO labor (guild_id, user_id) VALUES (?, ?)", (self._g(), user_id))
        row = dict(
            self._conn.execute("SELECT * FROM labor WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
        )
        buffs = self._relic_buffs(user_id)
        row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now, buffs.get("recovery", 0.0))
        row["fatigue_at"] = now
        if row["day"] != day:
            row["day"], row["day_earned"], row["day_click"] = day, 0, 0
        self._idle_settle(user_id, row, now, rng)
        return row

    def _relic_buffs(self, user_id: int) -> dict:
        """持っている発掘品の効果 {効果の名前: 上がり幅}（個数ぶん重なる。出品中の品は持ち物にないので、効かない）。"""
        held = {
            r["item_id"]: r["qty"]
            for r in self._conn.execute(
                "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0 AND item_id LIKE 'relic_%'",
                (self._g(), user_id),
            )
        }
        return relics_mod.buffs_of(held)

    def labor_donated_total(self) -> int:
        """このサーバーの全員の、慈善団体への寄付（帝愛のピンハネ）の合計。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COALESCE(SUM(donated), 0) FROM labor WHERE guild_id=?", (self._g(),)
            ).fetchone()[0]

    def relic_stock(self, user_id: int) -> dict:
        """持ち物にある発掘品 {品のID: 個数}（★つきも含む）。"""
        with self._lock:
            return {
                r["item_id"]: r["qty"]
                for r in self._conn.execute(
                    "SELECT item_id, qty FROM inventory WHERE guild_id=? AND user_id=? AND qty>0 AND item_id LIKE 'relic_%'",
                    (self._g(), user_id),
                )
            }

    def relic_merge(self, user_id: int, item_id: str) -> tuple:
        """同じ発掘品を MERGE_COUNT 個つかって、★が1つ増えた1個に合体する。(成功か, 理由か合体後の品のID)。"""
        base, star = relics_mod.split_id(item_id)
        if base not in relics_mod.BUFFS:
            return False, "発掘品ではありません。"
        if star >= relics_mod.STAR_MAX:
            return False, f"★{relics_mod.STAR_MAX} は最上位で、これ以上は合体できません。"
        new_id = relics_mod.star_id(base, star + 1)
        with self._lock, self._conn:
            self._ensure(user_id)
            cur = self._conn.execute(
                "UPDATE inventory SET qty = qty - ? WHERE guild_id=? AND user_id=? AND item_id=? AND qty >= ?",
                (relics_mod.MERGE_COUNT, self._g(), user_id, item_id, relics_mod.MERGE_COUNT),
            )
            if cur.rowcount == 0:
                return False, f"合体には、同じ発掘品が {relics_mod.MERGE_COUNT}個 要ります（出品中の品は数えません）。"
            self._conn.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + 1",
                (self._g(), user_id, new_id),
            )
        return True, new_id

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

    FINDS_KEEP = 40  # 1人ぶん残す、掘り出しの履歴の数

    def _labor_log_finds(self, user_id: int, finds: list) -> None:
        """掘り出しの履歴を書く（古いものは、FINDS_KEEP 件まで残して消す）。"""
        if not finds:
            return
        self._conn.executemany(
            "INSERT INTO labor_finds (guild_id, user_id, at, kind, ore, qty) VALUES (?, ?, ?, ?, ?, ?)",
            [(self._g(), user_id, at, kind, ore, n) for at, kind, ore, n in finds],
        )
        self._conn.execute(
            "DELETE FROM labor_finds WHERE guild_id=? AND user_id=? AND id NOT IN "
            "(SELECT id FROM labor_finds WHERE guild_id=? AND user_id=? ORDER BY at DESC, id DESC LIMIT ?)",
            (self._g(), user_id, self._g(), user_id, self.FINDS_KEEP),
        )

    def labor_finds(self, user_id: int, limit: int = 6) -> list:
        """最近の掘り出し（石炭は除く）。新しい順。[{at, kind, ore}]。"""
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT at, kind, ore FROM labor_finds WHERE guild_id=? AND user_id=? AND ore NOT IN (%s) "
                    "ORDER BY at DESC, id DESC LIMIT ?" % ",".join("?" * len(labor_mod.FLAT)),
                    (self._g(), user_id, *labor_mod.FLAT, limit),
                )
            ]

    @staticmethod
    def _relics_of(row: dict) -> dict:
        """受け取る前の発掘品 {品のID: 個数}。"""
        try:
            return {k: int(n) for k, n in json.loads(row.get("relics") or "{}").items() if k in shop_mod.BY_ID}
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
        """作業員の稼ぎを pot に足し、切れた契約を決算する（更新なら契約料を払い、払えなければ退職）。"""
        workers = self._workers_of(user_id)
        if workers:
            bal = self._conn.execute(
                "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            plan = labor_mod.settle_plan(
                workers,
                bool(row["renew"]),
                row["idle_at"],
                now,
                row["pot"],
                bal["balance"] if bal else 0,
                rng,
                wage_bonus=self._relic_buffs(user_id).get("wage", 0.0),
                luck_bonus=self._relic_buffs(user_id).get("luck", 0.0),
            )
            if plan["balance_spent"] > 0:
                self._apply(user_id, -plan["balance_spent"], "地下作業:契約の更新")
            for wid, expires in plan["renewed"]:
                self._conn.execute("UPDATE labor_workers SET expires_at=? WHERE id=?", (expires, wid))
            for wid in plan["removed"]:
                self._conn.execute("DELETE FROM labor_workers WHERE id=?", (wid,))
            row["pot"] += plan["gain"] - plan["pot_spent"]
            row["pot_workers"] = min(row["pot"], max(0, row["pot_workers"] + plan["gain"] - plan["pot_spent"]))
            if plan["relics"]:
                held = self._relics_of(row)
                for k, n in plan["relics"].items():
                    held[k] = held.get(k, 0) + n
                row["relics"] = json.dumps(held)
            if plan["ores"]:
                found = self._ores_of(row)
                for k, n in plan["ores"].items():
                    found[k] = found.get(k, 0) + n
                row["ores"] = json.dumps(found)
            self._labor_log_finds(user_id, plan["finds"])
            row["day_earned"] += plan["gain"]
            row["earned_total"] += plan["gain"]
            workers = self._workers_of(user_id)
        self._set_counts(row, workers)
        row["idle_at"] = now

    def _labor_save(self, user_id: int, row: dict) -> None:
        self._conn.execute(
            "UPDATE labor SET pick=?, glove=?, fatigue=?, fatigue_at=?, pot=?, day=?, day_earned=?, day_click=?, "
            "last_click=?, clicks=?, earned_total=?, dorm=?, idle_at=?, renew=?, ores=?, pot_workers=?, relics=?, donated=? "
            "WHERE guild_id=? AND user_id=?",
            (
                row["pick"],
                row["glove"],
                row["fatigue"],
                row["fatigue_at"],
                row["pot"],
                row["day"],
                row["day_earned"],
                row["day_click"],
                row["last_click"],
                row["clicks"],
                row["earned_total"],
                row["dorm"],
                row["idle_at"],
                row["renew"],
                row.get("ores", ""),
                row["pot_workers"],
                row.get("relics", ""),
                row.get("donated", 0),
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
        skimmed = teiai.labor_skim(amount)  # 地下労働なので、帝愛がピンハネする（借金の有無に関わらず）
        net = amount - skimmed
        if skimmed:
            self._ledger(
                user_id, 0, self._balance(user_id), f"慈善団体への寄付（地下作業のピンハネ {skimmed:,}ペリカ）"
            )
            self._house_income(skimmed)  # 帝愛の胴元の口座に入る
        self._apply(user_id, net, "地下作業:稼ぎ", income=net)
        row["pot"] = 0
        row["pot_workers"] = 0
        row["ores"] = ""
        row["skimmed"] = skimmed
        row["donated"] = row.get("donated", 0) + skimmed
        return net

    def labor_state(self, user_id: int, now: Optional[float] = None, day: str = "") -> dict:
        """いまの状態（画面用）。口座や行は、まだ無ければ作らない（読むだけ）。契約の決算は、したことにして見せる（書き込まない）。"""
        now = time.time() if now is None else now

        class _NoRoll:  # 見込みの表示では、鉱石の抽選をしない（random() が 1.0 なら、何も出ない）
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
                else {
                    k: 0
                    for k in (
                        "pick",
                        "glove",
                        "pot",
                        "pot_workers",
                        "day_earned",
                        "day_click",
                        "clicks",
                        "earned_total",
                        "dorm",
                        "idle_at",
                    )
                }
                | {
                    "fatigue": 0.0,
                    "fatigue_at": now,
                    "last_click": 0.0,
                    "day": day,
                    "renew": 1,
                    "ores": "",
                    "relics": "",
                    "donated": 0,
                }
            )
            buffs = self._relic_buffs(user_id)
            row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now, buffs.get("recovery", 0.0))
            if row["day"] != day:
                row["day_earned"] = row["day_click"] = 0
            bal = self._conn.execute(
                "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            row["balance"] = bal["balance"] if bal else 0
            workers = self._workers_of(user_id)
            plan = labor_mod.settle_plan(
                workers,
                bool(row["renew"]),
                row["idle_at"],
                now,
                row["pot"],
                row["balance"],
                _NoRoll,
                wage_bonus=buffs.get("wage", 0.0),
                luck_bonus=buffs.get("luck", 0.0),
            )
            gone = set(plan["removed"])
            renewed = dict(plan["renewed"])
            stay = [{**w, "expires_at": renewed.get(w["id"], w["expires_at"])} for w in workers if w["id"] not in gone]
            self._set_counts(row, stay)
            row["workers"] = len(stay)
            row["slots"] = labor_mod.slots(row["dorm"])
            row["rate"] = labor_mod.idle_rate(self._counts(row))
            row["relics"] = self._relics_of(row)  # 決算ずみの発掘品
            row["ores"] = self._ores_of(row)  # 決算ずみの鉱石（見込みの分は、掘るまで分からない）
            row["idle_pending"] = plan["gain"]  # まだ数えていない、作業員の稼ぎ（見込み）
            row["pot_total"] = row["pot"] + plan["gain"] - plan["pot_spent"]
            row["pot_workers_total"] = min(
                row["pot_total"], max(0, row["pot_workers"] + plan["gain"] - plan["pot_spent"])
            )  # 未受け取りのうち、作業員の分（まだ数えていない見込みも含む）
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
            row["finds"] = self.labor_finds(user_id)
            row["held_relics"] = self.relic_stock(user_id)
            row["donated_all"] = self.labor_donated_total()  # このサーバーの全員の寄付の合計
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
            pay = max(1, relics_mod.chance_round(out["pay"] * (1 + self._relic_buffs(user_id).get("click", 0.0))))
            room = labor_mod.MAX_CLICKS_PER_DAY - row["day_click"]  # 今日、あと何回、「働く」を押せるか
            pay = labor_mod.OVER_CAP_PAY if room <= 0 else pay
            hour = int(now // 3600)
            since = now - labor_mod.SUSPECT_HOURS * 3600
            suspected = labor_mod.auto_click_suspected(self.labor_click_hours(user_id, since), hour)
            if suspected:  # 自動操作の疑い: BANではなく、その日はそこで打ち止め（翌日には戻る）
                pay = labor_mod.OVER_CAP_PAY
            row["last_click_before"] = row["last_click"]
            row.update(
                fatigue=out["fatigue"],
                last_click=now,
                clicks=row["clicks"] + 1,
                pot=row["pot"] + pay,
                day_earned=row["day_earned"] + pay,
                day_click=row["day_click"] + 1,
                earned_total=row["earned_total"] + pay,
            )
            self._labor_log_click(user_id, now, pay, now - row["last_click_before"])
            self._labor_save(user_id, row)
            return {"ok": True, "pay": pay}

    CLICK_LOG_DAYS = 14  # 押した記録（1時間ごと）を残す日数

    def _labor_log_click(self, user_id: int, now: float, pay: int, gap: float) -> None:
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
                self._g(),
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

    def labor_click_hours(self, user_id: int, since: float) -> list:
        """since 以降の、1時間ごとの押した記録（古い順）。"""
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT hour, clicks, pay, gaps, sum_gap, sum_gap2, min_gap FROM labor_click_hours "
                    "WHERE guild_id=? AND user_id=? AND hour>=? ORDER BY hour",
                    (self._g(), user_id, int(since // 3600)),
                )
            ]

    def labor_collect(self, user_id: int, now: Optional[float] = None, day: str = "", detail: bool = False):
        """受け取る前の稼ぎから、帝愛のピンハネを引いて、残高に移す。移した額（借金の天引きの前）を返す。

        detail=True なら {amount（移した額）, skimmed（帝愛のピンハネ）, donated（これまでの寄付の合計）, ores（掘った鉱石）, relics（見つけた発掘品）}。
        発掘品は、ペリカにならず、持ち物に入る（稼ぎが 0 でも、受け取れる）。
        """
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self._labor_row(user_id, now, day)
            ores = self._ores_of(row)
            relics = self._relics_of(row)
            row["skimmed"] = 0
            amount = self._labor_flush(user_id, row)
            for item_id, n in relics.items():
                self._conn.execute(
                    "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty = qty + excluded.qty",
                    (self._g(), user_id, item_id, n),
                )
            row["relics"] = ""
            self._labor_save(user_id, row)
            if not detail:
                return amount
            return {
                "amount": amount,
                "skimmed": row["skimmed"],
                "donated": row["donated"],  # この人のこれまでの寄付の合計
                "donated_all": self.labor_donated_total(),  # このサーバーの全員の寄付の合計
                "ores": ores,
                "relics": relics,
            }

    def labor_drink(self, user_id: int, now: Optional[float] = None, day: str = "") -> tuple:
        """栄養ドリンコを1本飲んで、疲労を DRINK_RECOVER 回復する。(ok, 理由) を返す。疲れていなければ、減らさない。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self._labor_row(user_id, now, day)
            if row["fatigue"] < 1:
                self._labor_save(user_id, row)
                return False, "疲れていません。"
            if not self._consume(user_id, shop_mod.LABOR_DRINK):
                self._labor_save(user_id, row)
                return False, "栄養ドリンコを持っていません（ショップの消耗品で買えます）。"
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

    def labor_set_renew(self, user_id: int, on: bool, now: Optional[float] = None, day: str = "") -> tuple:
        """契約の自動更新を、オン・オフにする。(ok, 理由) を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._labor_row(user_id, now, day)
            row["renew"] = 1 if on else 0
            self._labor_save(user_id, row)
            return True, ""
