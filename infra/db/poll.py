"""投票（/投票）の記録。票・票数の書き換え（有料）・締切。

表の定義は infra/db/schema/tables.py（polls・poll_votes）。数字は domain/poll.py。
"""

from domain import poll
from domain.errors import Outcome, fail
from domain.ports.contracts import PollRigResult, PollRow, PollView
from domain.ports.repos import IPollRepo
from infra.db.account import AccountCore
from infra.db.bank import BankRepo
from infra.db.base import EcoDb, ScopedRepo
from infra.db.discord_posts import init_discord_post, set_discord_message

_SELECT_POLL = (
    "SELECT pl.id, pl.guild_id, pl.creator, pl.title, pl.closes, "
    "COALESCE(p.channel_id, 0) AS channel_id, COALESCE(p.message_id, 0) AS message_id, "
    "pl.adj_yes, pl.adj_no, pl.rigs, pl.status "
    "FROM polls pl "
    "LEFT JOIN discord_posts p ON p.entity_type = 'poll' AND p.entity_id = pl.id"
)


class PollRepo(ScopedRepo, IPollRepo):
    """投票の作成・票・書き換え・締切。"""

    def __init__(
        self, db: EcoDb, account: AccountCore, bank: BankRepo, guild_id: int = 0, strict: bool = False
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._bank = bank

    def _real_for(self, poll_id: int) -> tuple[int, int]:
        row = self._one(
            "SELECT COALESCE(SUM(yes), 0) AS yes, COUNT(*) AS n FROM poll_votes WHERE poll_id=?", (poll_id,)
        )
        assert row is not None  # 集計なので必ず1行返る
        return row["yes"], row["n"] - row["yes"]

    def _view_for(self, guild_id: int, poll_id: int) -> PollView | None:
        d = self._one(f"{_SELECT_POLL} WHERE pl.guild_id=? AND pl.id=?", (guild_id, poll_id))
        if d is None:
            return None
        real_yes, real_no = self._real_for(poll_id)
        d["voters"] = real_yes + real_no
        d["yes"] = poll.shown(real_yes, d["adj_yes"])
        d["no"] = poll.shown(real_no, d["adj_no"])
        return d

    def _open_for(self, guild_id: int, poll_id: int, now: float) -> PollRow | None:
        """受付中の投票の行。締切を過ぎていれば（見回りの前でも）None。"""
        return self._one(
            f"{_SELECT_POLL} WHERE pl.guild_id=? AND pl.id=? AND pl.status='open' AND pl.closes>?", (guild_id, poll_id, now)
        )

    def poll_create(self, creator: int, title: str, closes: float, channel_id: int, now: float) -> Outcome[int]:
        """投票を作る。(成功か, 番号 か Failure)。"""
        title = title.strip()
        if not title or len(title) > poll.TITLE_MAX:
            return False, fail("poll_bad_title", max=poll.TITLE_MAX)
        if not now < closes <= now + poll.MAX_MINUTES * 60:
            return False, fail("poll_bad_duration", max_minutes=poll.MAX_MINUTES)
        with self._db.tx():
            gid = self._g()
            mine = self._scalar(
                "SELECT COUNT(*) FROM polls WHERE guild_id=? AND creator=? AND status='open'", (gid, creator)
            )
            if mine >= poll.MAX_OPEN_PER_USER:
                return False, fail("poll_too_many", max=poll.MAX_OPEN_PER_USER)
            cur = self._conn.execute(
                "INSERT INTO polls (guild_id, creator, title, closes) VALUES (?, ?, ?, ?)",
                (gid, creator, title, closes),
            )
            lastrowid = cur.lastrowid
            assert lastrowid is not None
            init_discord_post(self._conn, "poll", lastrowid, channel_id)
            return True, lastrowid

    def poll_set_message(self, poll_id: int, channel_id: int, message_id: int) -> None:
        with self._db.tx():
            if self._one("SELECT 1 FROM polls WHERE id=? AND guild_id=?", (poll_id, self._g())) is None:
                return
            set_discord_message(self._conn, "poll", poll_id, message_id, channel_id)

    def poll_get(self, poll_id: int) -> PollView | None:
        with self._lock:
            return self._view_for(self._g(), poll_id)

    def poll_vote(self, user_id: int, poll_id: int, yes: bool, now: float) -> Outcome[PollView]:
        """1人1票。あとから変えられる。(成功か, 投票 か Failure)。"""
        with self._db.tx():
            gid = self._g()
            if self._open_for(gid, poll_id, now) is None:
                return False, fail("poll_closed")
            self._conn.execute(
                "INSERT INTO poll_votes (poll_id, user_id, yes) VALUES (?, ?, ?) "
                "ON CONFLICT(poll_id, user_id) DO UPDATE SET yes=excluded.yes",
                (poll_id, user_id, int(yes)),
            )
            view = self._view_for(gid, poll_id)
            assert view is not None  # 直前に open を確認済み
            return True, view

    def poll_rig(self, user_id: int, poll_id: int, yes: int, no: int, now: float) -> Outcome[PollRigResult]:
        """作った人だけが、RIG_COST を払って、表示する票数を yes・no に書き換えられる。代金は帝愛銀行の収入。

        (成功か, {"poll": 書き換え後, "before": (賛成, 反対)} か Failure)。あとから入る本物の票は、書き換えた数に足される。
        """
        if not (0 <= yes <= poll.MAX_COUNT and 0 <= no <= poll.MAX_COUNT):
            return False, fail("poll_bad_count", max=poll.MAX_COUNT)
        with self._db.tx():
            gid = self._g()
            row = self._open_for(gid, poll_id, now)
            if row is None:
                return False, fail("poll_closed")
            if row["creator"] != user_id:
                return False, fail("poll_not_creator")
            self._account.ensure(gid, user_id)
            bal = self._account.raw_balance(gid, user_id)
            if bal < poll.RIG_COST:
                return False, fail("insufficient_balance", balance=bal)
            before = self._view_for(gid, poll_id)
            if before is None:
                return False, fail("poll_closed")
            self._account.apply(gid, user_id, -poll.RIG_COST, "投票:票の書き換え")
            self._bank.bank_income_for(gid, poll.RIG_COST, now)
            real_yes, real_no = self._real_for(poll_id)
            self._conn.execute(
                "UPDATE polls SET adj_yes=?, adj_no=?, rigs=rigs+1 WHERE guild_id=? AND id=?",
                (yes - real_yes, no - real_no, gid, poll_id),
            )
            after = self._view_for(gid, poll_id)
            assert after is not None  # 直前まで開いていた投票が、書き換えで閉じることはない
            result: PollRigResult = {"poll": after, "before": (before["yes"], before["no"])}
            return True, result

    def poll_close_due(self, now: float) -> list[PollView]:
        """締切を過ぎた投票を締めて、締めたものを返す。"""
        with self._db.tx():
            gid = self._g()
            ids = [
                r["id"]
                for r in self._all(
                    "SELECT id FROM polls WHERE guild_id=? AND status='open' AND closes<=? ORDER BY id", (gid, now)
                )
            ]
            out = []
            for poll_id in ids:
                self._conn.execute("UPDATE polls SET status='closed' WHERE id=?", (poll_id,))
                view = self._view_for(gid, poll_id)
                if view is not None:
                    out.append(view)
            return out
