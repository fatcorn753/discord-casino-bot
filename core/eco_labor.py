"""地下作業（Economy に混ぜ込む部品）。クリッカー。数字と計算は core/labor.py、表は core/schema.py の labor。

稼ぎは、まず「受け取る前の稼ぎ（pot）」に貯まる。AUTO_COLLECT に届くか、受け取りを押すと、残高に移る（収入なので、借金があれば天引き）。
上限や、手元の制限はない。day_earned は、画面に出す「今日の稼ぎ」。
"""

import random
import time
from typing import Optional

from core import labor as labor_mod


class LaborMixin:
    def _labor_row(self, user_id: int, now: float, day: str) -> dict:
        """行を読む（無ければ作る）。日が変わっていたら、今日の稼ぎを 0 に戻し、疲労は時間で回復させる。呼び出し側のトランザクション内で使う。"""
        self._conn.execute("INSERT OR IGNORE INTO labor (guild_id, user_id) VALUES (?, ?)", (self._g(), user_id))
        row = dict(
            self._conn.execute("SELECT * FROM labor WHERE guild_id=? AND user_id=?", (self._g(), user_id)).fetchone()
        )
        row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now)
        row["fatigue_at"] = now
        if row["day"] != day:
            row["day"], row["day_earned"] = day, 0
        return row

    def _labor_save(self, user_id: int, row: dict) -> None:
        self._conn.execute(
            "UPDATE labor SET pick=?, glove=?, helmet=?, fatigue=?, fatigue_at=?, pot=?, day=?, day_earned=?, "
            "last_click=?, rest_until=?, clicks=?, earned_total=? WHERE guild_id=? AND user_id=?",
            (
                row["pick"],
                row["glove"],
                row["helmet"],
                row["fatigue"],
                row["fatigue_at"],
                row["pot"],
                row["day"],
                row["day_earned"],
                row["last_click"],
                row["rest_until"],
                row["clicks"],
                row["earned_total"],
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
                else {k: 0 for k in ("pick", "glove", "helmet", "pot", "day_earned", "clicks", "earned_total")}
                | {"fatigue": 0.0, "fatigue_at": now, "last_click": 0.0, "rest_until": 0.0, "day": day}
            )
            row["fatigue"] = labor_mod.fatigue_now(row["fatigue"], row["fatigue_at"], now)
            if row["day"] != day:
                row["day_earned"] = 0
            bal = self._conn.execute(
                "SELECT balance FROM users WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            row["balance"] = bal["balance"] if bal else 0
            row["resting"] = max(0.0, row["rest_until"] - now)
            return row

    def labor_click(
        self, user_id: int, now: Optional[float] = None, day: str = "", rng: Optional[random.Random] = None
    ) -> dict:
        """「働く」を1回押す。{ok, reason, accident, pay, collected}。ok=False のときは、何も変わらない。"""
        now = time.time() if now is None else now
        rng = rng or random.SystemRandom()
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._labor_row(user_id, now, day)
            if row["rest_until"] > now:
                return {"ok": False, "reason": f"休憩中です（あと {row['rest_until'] - now:.0f} 秒）。"}
            if now - row["last_click"] < labor_mod.CLICK_INTERVAL:
                return {"ok": False, "reason": "押すのが早すぎます。", "quiet": True}
            out = labor_mod.click_outcome(row["pick"], row["glove"], row["helmet"], row["fatigue"], rng)
            pay = out["pay"]
            row.update(
                fatigue=out["fatigue"],
                last_click=now,
                clicks=row["clicks"] + 1,
                pot=row["pot"] + pay,
                day_earned=row["day_earned"] + pay,
                earned_total=row["earned_total"] + pay,
            )
            collected = self._labor_flush(user_id, row) if row["pot"] >= labor_mod.AUTO_COLLECT else 0
            self._labor_save(user_id, row)
            return {"ok": True, "accident": out["accident"], "pay": pay, "collected": collected}

    def labor_collect(self, user_id: int, now: Optional[float] = None, day: str = "") -> int:
        """受け取る前の稼ぎを、残高に移す。移した額（天引きの前）を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self._labor_row(user_id, now, day)
            amount = self._labor_flush(user_id, row)
            self._labor_save(user_id, row)
            return amount

    def labor_rest(self, user_id: int, now: Optional[float] = None, day: str = "") -> tuple:
        """休憩する。疲労が REST_RECOVER 回復し、REST_SECONDS の間、押せない。(ok, 理由) を返す。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            row = self._labor_row(user_id, now, day)
            if row["rest_until"] > now:
                return False, "もう休憩中です。"
            if row["fatigue"] < 1:
                return False, "疲れていません。"
            row["fatigue"] = max(0.0, row["fatigue"] - labor_mod.REST_RECOVER)
            row["rest_until"] = now + labor_mod.REST_SECONDS
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
