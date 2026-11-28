"""配信（/配信）の記録。配信パネル・スパチャの合計・勝敗の賭け（預かり・山分け・返金）。

表の定義は infra/db/schema/tables.py（streams・stream_rounds・stream_bets）。数字と山分けの計算は domain/stream.py。
スパチャのお金の移動は送金（WalletRepo.transfer）で行い、ここでは合計だけを記録する。
"""

from typing import cast

from domain import stream
from domain.errors import Outcome, fail
from domain.ports.contracts import StreamRoundView, StreamRow, StreamSettleResult, StreamView
from domain.ports.repos import IStreamRepo
from infra.db.account import AccountCore
from infra.db.bank import BankRepo
from infra.db.base import EcoDb, ScopedRepo
from infra.db.discord_posts import init_discord_post, set_discord_message

ACTIVE = ("open", "locked")  # 決まっていない勝負の状態

_SELECT_STREAM = (
    "SELECT s.id, s.guild_id, s.streamer, s.title, s.url, "
    "COALESCE(p.channel_id, 0) AS channel_id, COALESCE(p.message_id, 0) AS message_id, "
    "s.started, s.ends, s.tips, s.tip_count, s.status, "
    "COALESCE(vc.voice_channel_id, 0) AS voice_channel_id, COALESCE(vc.created_vc, 0) AS created_vc "
    "FROM streams s "
    "LEFT JOIN discord_posts p ON p.entity_type = 'stream' AND p.entity_id = s.id "
    "LEFT JOIN stream_voice_channels vc ON vc.stream_id = s.id"
)


class StreamRepo(ScopedRepo, IStreamRepo):
    """配信の開始・スパチャの合計・勝負の受付と精算・配信の終了。"""

    def __init__(
        self, db: EcoDb, account: AccountCore, bank: BankRepo, guild_id: int = 0, strict: bool = False
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._bank = bank

    # ------------------------------ 読む ------------------------------ #
    def _round_view(self, stream_id: int) -> StreamRoundView | None:
        """その配信の、決まっていない勝負（あれば1つ）と、いまの賭けの合計。"""
        r = self._one(
            "SELECT * FROM stream_rounds WHERE stream_id=? AND status IN ('open','locked') ORDER BY id DESC LIMIT 1",
            (stream_id,),
        )
        if r is None:
            return None
        sums = self._one(
            "SELECT COALESCE(SUM(CASE WHEN side='win' THEN amount END), 0) AS w, "
            "COALESCE(SUM(CASE WHEN side='lose' THEN amount END), 0) AS l, COUNT(*) AS n "
            "FROM stream_bets WHERE round_id=?",
            (r["id"],),
        )
        assert sums is not None  # 集計なので必ず1行返る
        r.pop("settled", None)
        r.update(win_total=sums["w"], lose_total=sums["l"], bettors=sums["n"])
        return cast("StreamRoundView", r)

    def _view(self, guild_id: int, stream_id: int) -> StreamView | None:
        row = self._one(f"{_SELECT_STREAM} WHERE s.guild_id=? AND s.id=?", (guild_id, stream_id))
        if row is None:
            return None
        view = cast("StreamView", row)
        view["round"] = self._round_view(stream_id)
        return view

    def _live_of(self, guild_id: int, streamer: int, stream_id: int) -> StreamRow | None:
        """配信者本人の、配信中のパネル。違う人・終わった配信なら None。"""
        return self._one(
            f"{_SELECT_STREAM} WHERE s.guild_id=? AND s.id=? AND s.streamer=? AND s.status='live'",
            (guild_id, stream_id, streamer),
        )

    def stream_get(self, stream_id: int) -> StreamView | None:
        with self._lock:
            return self._view(self._g(), stream_id)

    # ------------------------------ 配信 ------------------------------ #
    def stream_start(self, streamer: int, title: str, url: str, channel_id: int, now: float) -> Outcome[int]:
        """配信パネルを作る。(成功か, 番号 か Failure)。1人が同時に開けるのは1つ。"""
        title = title.strip()
        if not title or len(title) > stream.TITLE_MAX:
            return False, fail("stream_bad_title", max=stream.TITLE_MAX)
        url = url.strip()
        if url and not url.startswith(("https://", "http://")):
            return False, fail("stream_bad_url")
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, streamer)
            if self._one("SELECT 1 FROM streams WHERE guild_id=? AND streamer=? AND status='live'", (gid, streamer)):
                return False, fail("stream_already_live")
            cur = self._conn.execute(
                "INSERT INTO streams (guild_id, streamer, title, url, started, ends) VALUES (?, ?, ?, ?, ?, ?)",
                (gid, streamer, title, url, now, now + stream.MAX_HOURS * 3600),
            )
            assert cur.lastrowid is not None
            init_discord_post(self._conn, "stream", cur.lastrowid, channel_id)
            return True, cur.lastrowid

    def stream_set_message(self, stream_id: int, channel_id: int, message_id: int) -> None:
        with self._db.tx():
            if self._one("SELECT 1 FROM streams WHERE id=? AND guild_id=?", (stream_id, self._g())) is None:
                return
            set_discord_message(self._conn, "stream", stream_id, message_id, channel_id)

    def stream_set_voice(self, stream_id: int, voice_channel_id: int, created: bool) -> None:
        """配信している場所（ボイスチャンネル）を記録する。created は Bot が作った VC か（閉じたら消す）。"""
        with self._db.tx():
            if self._one("SELECT 1 FROM streams WHERE id=? AND guild_id=?", (stream_id, self._g())) is None:
                return
            if voice_channel_id <= 0:
                self._conn.execute("DELETE FROM stream_voice_channels WHERE stream_id=?", (stream_id,))
                return
            self._conn.execute(
                "INSERT INTO stream_voice_channels (stream_id, voice_channel_id, created_vc) VALUES (?, ?, ?) "
                "ON CONFLICT(stream_id) DO UPDATE SET "
                "voice_channel_id=excluded.voice_channel_id, created_vc=excluded.created_vc",
                (stream_id, voice_channel_id, int(created)),
            )

    def stream_add_tip(self, stream_id: int, amount: int) -> None:
        """スパチャの合計に足す（お金の移動は、送金で済んでいる）。"""
        with self._db.tx():
            self._conn.execute(
                "UPDATE streams SET tips = tips + ?, tip_count = tip_count + 1 WHERE guild_id=? AND id=?",
                (amount, self._g(), stream_id),
            )

    # ------------------------------ 勝負 ------------------------------ #
    def stream_round_open(self, streamer: int, stream_id: int, label: str, now: float) -> Outcome[StreamView]:
        """勝負を始めて、賭けの受付を開く。決まっていない勝負があるうちは、次を始められない。"""
        label = label.strip() or "次の勝負"
        if len(label) > stream.ROUND_LABEL_MAX:
            return False, fail("stream_bad_label", max=stream.ROUND_LABEL_MAX)
        with self._db.tx():
            gid = self._g()
            if self._live_of(gid, streamer, stream_id) is None:
                return False, fail("stream_not_streamer")
            if self._round_view(stream_id) is not None:
                return False, fail("stream_round_active")
            self._conn.execute(
                "INSERT INTO stream_rounds (stream_id, guild_id, label, created) VALUES (?, ?, ?, ?)",
                (stream_id, gid, label, now),
            )
            view = self._view(gid, stream_id)
            assert view is not None
            return True, view

    def stream_round_lock(self, streamer: int, stream_id: int) -> Outcome[StreamView]:
        """賭けの受付を締め切る（勝負が始まったら押す）。"""
        with self._db.tx():
            gid = self._g()
            if self._live_of(gid, streamer, stream_id) is None:
                return False, fail("stream_not_streamer")
            r = self._round_view(stream_id)
            if r is None or r["status"] != "open":
                return False, fail("stream_no_open_round")
            self._conn.execute("UPDATE stream_rounds SET status='locked' WHERE id=?", (r["id"],))
            view = self._view(gid, stream_id)
            assert view is not None
            return True, view

    def stream_bet(self, user_id: int, stream_id: int, side: str, amount: int, now: float) -> Outcome[StreamView]:
        """賭ける。賭け金は、精算まで預かる。同じ側なら、上限まで足せる（反対の側には変えられない）。"""
        if side not in stream.SIDES:
            return False, fail("stream_bad_side")
        if amount < stream.BET_MIN:
            return False, fail("stream_bet_min", min=stream.BET_MIN)
        with self._db.tx():
            gid = self._g()
            s = self._one("SELECT * FROM streams WHERE guild_id=? AND id=? AND status='live'", (gid, stream_id))
            r = self._round_view(stream_id) if s else None
            if s is None or r is None or r["status"] != "open":
                return False, fail("stream_no_open_round")
            if s["streamer"] == user_id:
                return False, fail("stream_self_bet")
            self._account.ensure(gid, user_id)
            debt = self._account.debt(gid, user_id)
            if debt > 0:
                return False, fail("debt_blocks", debt=debt, verb="賭けられません")
            mine = self._one("SELECT side, amount FROM stream_bets WHERE round_id=? AND user_id=?", (r["id"], user_id))
            if mine and mine["side"] != side:
                return False, fail("stream_other_side")
            already = mine["amount"] if mine else 0
            if already + amount > stream.BET_MAX:
                return False, fail("stream_bet_max", max=stream.BET_MAX, already=already)
            bal = self._account.raw_balance(gid, user_id)
            if bal < amount:
                return False, fail("insufficient_balance_need", need=amount, balance=bal)
            self._account.apply(gid, user_id, -amount, f"配信の賭け（#{stream_id}）")
            self._conn.execute(
                "INSERT INTO stream_bets (round_id, user_id, side, amount) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(round_id, user_id) DO UPDATE SET amount = amount + excluded.amount",
                (r["id"], user_id, side, amount),
            )
            view = self._view(gid, stream_id)
            assert view is not None
            return True, view

    def _settle(
        self, gid: int, stream_id: int, r: StreamRoundView, winner: str | None, now: float
    ) -> StreamSettleResult:
        """勝負を精算する（トランザクションの中で呼ぶ）。当てた人に配当を、無効なら全員に賭け金を返す。"""
        bets = {
            b["user_id"]: (b["side"], b["amount"])
            for b in self._all("SELECT user_id, side, amount FROM stream_bets WHERE round_id=?", (r["id"],))
        }
        result = stream.settle(bets, winner)
        for uid, paid in result.payouts.items():
            stake = bets[uid][1]
            reason = f"配信の賭けの返金（#{stream_id}）" if result.refunded else f"配信の賭けの配当（#{stream_id}）"
            self._account.apply(gid, uid, paid, reason, income=0 if result.refunded else paid - stake)
        if result.rake:
            self._bank.bank_income_for(gid, result.rake, now)
        status = "void" if winner is None else winner
        self._conn.execute("UPDATE stream_rounds SET status=?, settled=? WHERE id=?", (status, now, r["id"]))
        view = self._view(gid, stream_id)
        assert view is not None
        return StreamSettleResult(
            stream=view,
            round_id=r["id"],
            label=r["label"],
            winner=winner,
            payouts=result.payouts,
            rake=result.rake,
            refunded=result.refunded,
        )

    def stream_round_settle(
        self, streamer: int, stream_id: int, winner: str | None, now: float
    ) -> Outcome[StreamSettleResult]:
        """配信者が勝敗を決める。winner は win / lose、None は無効（全額返す）。"""
        if winner is not None and winner not in stream.SIDES:
            return False, fail("stream_bad_side")
        with self._db.tx():
            gid = self._g()
            if self._live_of(gid, streamer, stream_id) is None:
                return False, fail("stream_not_streamer")
            r = self._round_view(stream_id)
            if r is None:
                return False, fail("stream_no_round")
            return True, self._settle(gid, stream_id, r, winner, now)

    def _end(self, gid: int, stream_id: int, now: float) -> StreamSettleResult | None:
        """配信を閉じる（トランザクションの中で呼ぶ）。決まっていない勝負は、無効にして全額返す。"""
        r = self._round_view(stream_id)
        voided = self._settle(gid, stream_id, r, None, now) if r is not None else None
        self._conn.execute("UPDATE streams SET status='ended', ends=? WHERE id=?", (now, stream_id))
        return voided

    def stream_end(self, streamer: int, stream_id: int, now: float) -> Outcome[StreamSettleResult | None]:
        """配信者が配信を終える。(成功か, 無効にした勝負の精算 か None か Failure)。"""
        with self._db.tx():
            gid = self._g()
            if self._live_of(gid, streamer, stream_id) is None:
                return False, fail("stream_not_streamer")
            return True, self._end(gid, stream_id, now)

    def stream_close_due(self, now: float) -> list[tuple[StreamView, StreamSettleResult | None]]:
        """時間切れの配信を閉じる（配信者が閉じ忘れても、預かった賭け金が戻るように）。"""
        out: list[tuple[StreamView, StreamSettleResult | None]] = []
        with self._db.tx():
            gid = self._g()
            for s in self._all("SELECT id FROM streams WHERE guild_id=? AND status='live' AND ends<=?", (gid, now)):
                voided = self._end(gid, s["id"], now)
                view = self._view(gid, s["id"])
                assert view is not None
                out.append((view, voided))
        return out
