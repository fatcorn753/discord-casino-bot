"""動議（信託の上乗せ金利。#94: いくつも並行でき、締めた順に後勝ち）。"""

from domain import kyosai
from domain.errors import Outcome, fail
from domain.ports.contracts import KyosaiMotionProposeRow, KyosaiMotionRow, KyosaiMotionViewRow
from infra.db.discord_posts import set_discord_message

from .offers import KyosaiRepo as Base

_SELECT_MOTION = (
    "SELECT m.id, m.guild_id, m.proposer, m.premium, m.created, m.closes, m.status, m.yes, m.no, "
    "COALESCE(p.channel_id, 0) AS channel_id, COALESCE(p.message_id, 0) AS message_id "
    "FROM kyosai_motions m "
    "LEFT JOIN discord_posts p ON p.entity_type = 'kyosai_motion' AND p.entity_id = m.id"
)


class KyosaiRepo(Base):
    def _open_motions_for(self, guild_id: int) -> list[KyosaiMotionRow]:
        return self._all(
            f"{_SELECT_MOTION} WHERE m.guild_id=? AND m.status='open' ORDER BY m.closes, m.id", (guild_id,)
        )

    def kyosai_propose(self, user_id: int, premium: float, now: float) -> Outcome[KyosaiMotionProposeRow]:
        """動議を出す。(成功か, Failure か動議の行)。

        通知の荒らしを防ぐため、口が MOTION_MIN_UNITS 以上の人だけが、1人 MOTION_MAX_OPEN_PER_USER 件まで、
        前に出してから MOTION_COOLDOWN_HOURS たってから出せる。提案した人は賛成に数える。
        """
        if not kyosai.premium_ok(premium):
            return False, fail("kyosai_bad_premium", max_pct=kyosai.PREMIUM_LIMIT * 100)
        with self._db.tx():
            gid = self._g()
            self.bank_sync(gid, now)
            units = self.units_of_for(gid, user_id)
            if units < kyosai.MOTION_MIN_UNITS:
                return False, fail("kyosai_motion_units", need=kyosai.MOTION_MIN_UNITS, have=units)
            mine = [m for m in self._open_motions_for(gid) if m["proposer"] == user_id]
            if len(mine) >= kyosai.MOTION_MAX_OPEN_PER_USER:
                return False, fail("kyosai_motion_open", max=kyosai.MOTION_MAX_OPEN_PER_USER)
            last = self._scalar(
                "SELECT MAX(created) FROM kyosai_motions WHERE guild_id=? AND proposer=?", (gid, user_id)
            )
            if last and now - last < kyosai.MOTION_COOLDOWN_HOURS * 3600:
                return False, fail("kyosai_motion_cooldown", until=last + kyosai.MOTION_COOLDOWN_HOURS * 3600)
            closes = now + kyosai.MOTION_HOURS * 3600
            cur = self._conn.execute(
                "INSERT INTO kyosai_motions (guild_id, proposer, premium, created, closes) VALUES (?, ?, ?, ?, ?)",
                (gid, user_id, premium, now, closes),
            )
            motion_id = cur.lastrowid
            assert motion_id is not None
            self._conn.execute(
                "INSERT INTO kyosai_votes (motion_id, user_id, yes) VALUES (?, ?, 1)", (motion_id, user_id)
            )
            proposed: KyosaiMotionProposeRow = {
                "id": motion_id,
                "proposer": user_id,
                "premium": premium,
                "closes": closes,
            }
            return True, proposed

    def kyosai_vote(self, user_id: int, yes: bool, now: float, motion_id: int | None = None) -> Outcome[int]:
        """投票中の動議に投票する（あとから変えられる）。番号を省けるのは、投票中が1件だけのとき。

        (成功か, Failure か動議の番号)。
        """
        with self._db.tx():
            gid = self._g()
            motions = [m for m in self._open_motions_for(gid) if m["closes"] > now]
            if motion_id is None:
                if not motions:
                    return False, fail("kyosai_no_motion")
                if len(motions) > 1:
                    return False, fail("kyosai_motion_ambiguous", ids=", ".join(f"#{m['id']}" for m in motions))
                motion = motions[0]
            else:
                motion = next((m for m in motions if m["id"] == motion_id), None)
                if motion is None:
                    return False, fail("kyosai_no_motion")
            if self.units_of_for(gid, user_id) <= 0:
                return False, fail("kyosai_no_units")
            self._conn.execute(
                "INSERT INTO kyosai_votes (motion_id, user_id, yes) VALUES (?, ?, ?) "
                "ON CONFLICT(motion_id, user_id) DO UPDATE SET yes=excluded.yes",
                (motion["id"], user_id, 1 if yes else 0),
            )
            return True, motion["id"]

    def _tally_for(self, guild_id: int, motion_id: int) -> tuple[int, int]:
        """賛成・反対の口数（締めるときの、いまの口数で数える。譲った口が二重に数えられないように）。"""
        yes = no = 0
        for r in self._all("SELECT user_id, yes FROM kyosai_votes WHERE motion_id=?", (motion_id,)):
            units = self.units_of_for(guild_id, r["user_id"])
            if r["yes"]:
                yes += units
            else:
                no += units
        return yes, no

    def _circulating_for(self, guild_id: int) -> int:
        """流通口数（帝愛の持ち口を除いた口の合計。可決に要る賛成の割合の分母）。"""
        return sum(u for _, u in self._holdings_for(guild_id, with_house=False))

    def kyosai_close_motions(self, now: float) -> list[KyosaiMotionViewRow]:
        """締切を過ぎた動議を、締切の順にすべて締める。可決なら、その順に信託の上乗せ金利を上書きする（後勝ち）。

        締めた動議の結果を、締めた順に返す（なければ空）。
        """
        out: list[KyosaiMotionViewRow] = []
        with self._db.tx():
            gid = self._g()
            circulating = self._circulating_for(gid)
            for motion in self._open_motions_for(gid):
                if motion["closes"] > now:
                    break
                yes, no = self._tally_for(gid, motion["id"])
                passed = kyosai.motion_passes(yes, no, circulating)
                status = "passed" if passed else "rejected"
                self._conn.execute(
                    "UPDATE kyosai_motions SET status=?, yes=?, no=? WHERE id=?", (status, yes, no, motion["id"])
                )
                if passed:
                    self.bank_sync(gid, now)
                    self._bank.set_trust_premium_for(gid, motion["premium"], now)
                out.append({**motion, "status": status, "yes": yes, "no": no, "circulating": circulating})
        return out

    def kyosai_set_motion_message(self, motion_id: int, channel_id: int, message_id: int) -> None:
        """動議の告知の投稿を覚えておく（締めたときに、結果へ書き換えてボタンを外すため）。"""
        with self._db.tx():
            if self._one("SELECT 1 FROM kyosai_motions WHERE id=? AND guild_id=?", (motion_id, self._g())) is None:
                return
            set_discord_message(self._conn, "kyosai_motion", motion_id, message_id, channel_id)

    def kyosai_motion(self, motion_id: int, now: float) -> KyosaiMotionViewRow | None:
        """動議1件と、いまの賛成・反対の口数（ボタンの投稿の書き換え用）。"""
        with self._lock:
            gid = self._g()
            row: KyosaiMotionRow | None = self._one(
                f"{_SELECT_MOTION} WHERE m.id=? AND m.guild_id=?", (motion_id, gid)
            )
            if row is None:
                return None
            yes, no = self._tally_for(gid, motion_id) if row["status"] == "open" else (row["yes"], row["no"])
            return {**row, "yes": yes, "no": no, "circulating": self._circulating_for(gid)}
