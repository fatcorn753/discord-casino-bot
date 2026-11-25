"""地下闘技場（NPC戦士どうしの賭け対戦）の永続化リポジトリ（SRP・Constructor DI）。

数字と決まりは `domain/arena.py`、表は `infra/db/schema/tables.py` の `arena_*`。
"""

import datetime
import random
from typing import Any

from domain import arena as arena_mod
from domain import teiai
from domain.errors import Outcome, fail
from domain.ports.bridges import IBankCashFlow
from domain.ports.contracts import (
    ArenaBetRow,
    ArenaFighterRow,
    ArenaFinalResult,
    ArenaGroupResult,
    ArenaSemiResult,
    ArenaTournamentDetailRow,
)
from domain.ports.repos import IArenaRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo


class ArenaRepo(ScopedRepo, IArenaRepo):
    """地下闘技場：1日1回、1on1とバトロワを両方開催する大会の生成・賭け・精算。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        bank: IBankCashFlow,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._bank = bank

    def arena_today(self, now: float, tz_hours: float) -> str:
        tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
        return datetime.datetime.fromtimestamp(now, tz).date().isoformat()

    def arena_tournament_ensure(
        self, fmt: str, now: float, tz_hours: float, rng: random.Random | None = None
    ) -> ArenaTournamentDetailRow:
        """今日の、指定した形式の大会を用意する（無ければ作る）。すでにあれば、そのまま返す。"""
        rng = rng or random.SystemRandom()
        day = self.arena_today(now, tz_hours)
        with self._db.tx():
            gid = self._g()
            row = self._one("SELECT id FROM arena_tournaments WHERE guild_id=? AND day=? AND format=?", (gid, day, fmt))
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO arena_tournaments (guild_id, day, format, status, created) "
                    "VALUES (?, ?, ?, 'semi_betting', ?)",
                    (gid, day, fmt, now),
                )
                tid = cur.lastrowid
                assert tid is not None
                fighters = arena_mod.generate_fighters(fmt, rng)
                block = arena_mod.group_size(fmt)
                for i, f in enumerate(fighters):
                    self._conn.execute(
                        "INSERT INTO arena_fighters (tournament_id, name, base_power, group_no, stage) "
                        "VALUES (?, ?, ?, ?, 'semi')",
                        (tid, f["name"], f["base_power"], i // block + 1),
                    )
                self._arena_seed_spectators(tid, gid, now, rng)
            result = self.arena_tournament_get_by_day(day, fmt)
            assert result is not None
            return result

    def arena_tournaments_ensure_today(
        self, now: float, tz_hours: float, rng: random.Random | None = None
    ) -> list[ArenaTournamentDetailRow]:
        """今日の大会を、1on1・バトロワの両方そろえて返す（無ければ作る）。"""
        return [self.arena_tournament_ensure(fmt, now, tz_hours, rng) for fmt in arena_mod.FORMATS]

    def arena_tournament_get_by_day(self, day: str, fmt: str) -> ArenaTournamentDetailRow | None:
        with self._lock:
            t = self._one(
                "SELECT * FROM arena_tournaments WHERE guild_id=? AND day=? AND format=?", (self._g(), day, fmt)
            )
            if t is None:
                return None
            t["fighters"] = self.arena_fighters(t["id"])
            return t

    def arena_fighters(self, tournament_id: int) -> list[ArenaFighterRow]:
        with self._lock:
            return self._all("SELECT * FROM arena_fighters WHERE tournament_id=? ORDER BY id", (tournament_id,))

    def _arena_seed_spectators(self, tournament_id: int, guild_id: int, now: float, rng: random.Random) -> None:
        """観客（プレイヤー以外）の初期ランダム賭けを仕込む。帝愛の手元現金から、上限つきで補う。"""
        state = self._bank.bank_sync_for(guild_id, now)
        topup_left = min(arena_mod.HOUSE_TOPUP_MAX, int(state["cash"]))
        spent = 0
        for f in self.arena_fighters(tournament_id):
            for _ in range(rng.randint(*arena_mod.SPECTATOR_COUNT)):
                amt = arena_mod.initial_spectator_bets(rng)
                if spent + amt > topup_left:
                    continue
                spent += amt
                self._conn.execute(
                    "INSERT INTO arena_npc_bets (tournament_id, stage, fighter_id, amount, created) "
                    "VALUES (?, 'semi', ?, ?, ?)",
                    (tournament_id, f["id"], amt, now),
                )
                self._conn.execute("UPDATE arena_fighters SET bet_total = bet_total + ? WHERE id=?", (amt, f["id"]))
        if spent:
            self._bank.bank_set_for(guild_id, state["cash"] - spent, state["loaned"])

    def arena_bet(self, user_id: int, fighter_id: int, amount: int, now: float) -> Outcome[str]:
        """戦士に賭ける。(成功か, Failure か空文字)。"""
        if amount < arena_mod.MIN_STAKE or amount > arena_mod.MAX_STAKE:
            return False, fail("arena_stake_range", lo=arena_mod.MIN_STAKE, hi=arena_mod.MAX_STAKE)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            fighter = self._one("SELECT * FROM arena_fighters WHERE id=?", (fighter_id,))
            if fighter is None:
                return False, fail("arena_fighter_not_found")
            tournament = self._one("SELECT * FROM arena_tournaments WHERE id=?", (fighter["tournament_id"],))
            if tournament is None or tournament["guild_id"] != gid:
                return False, fail("arena_fighter_not_found")
            if fighter["stage"] == "out":
                return False, fail("arena_fighter_eliminated")
            open_status = "semi_betting" if fighter["stage"] == "semi" else "final_betting"
            if tournament["status"] != open_status:
                return False, fail("arena_bet_closed")
            bal = self._account.raw_balance(gid, user_id)
            if bal < amount:
                return False, fail("insufficient_balance", balance=bal)
            self._account.apply(gid, user_id, -amount, f"闘技場:{fighter['name']}に賭け")
            self._conn.execute(
                "INSERT INTO arena_bets (tournament_id, stage, fighter_id, user_id, amount, created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tournament["id"], fighter["stage"], fighter_id, user_id, amount, now),
            )
            self._conn.execute("UPDATE arena_fighters SET bet_total = bet_total + ? WHERE id=?", (amount, fighter_id))
            return True, ""

    def _arena_settle_group(
        self,
        guild_id: int,
        tournament_id: int,
        stage: str,
        winner: ArenaFighterRow,
        losers: list[ArenaFighterRow],
        win_odds: float,
        now: float | None = None,
    ) -> None:
        """1試合ぶんを精算する。敗者側の賭け（観客ぶんも含む）が、勝者側のプレイヤーの配当の原資になる。"""
        loser_ids = [f["id"] for f in losers]
        losers_pool = 0
        if loser_ids:
            placeholders = ",".join("?" * len(loser_ids))
            player_losers = (
                self._scalar(
                    f"SELECT COALESCE(SUM(amount),0) FROM arena_bets WHERE tournament_id=? AND stage=? "
                    f"AND fighter_id IN ({placeholders})",
                    (tournament_id, stage, *loser_ids),
                )
                or 0
            )
            npc_losers = (
                self._scalar(
                    f"SELECT COALESCE(SUM(amount),0) FROM arena_npc_bets WHERE tournament_id=? AND stage=? "
                    f"AND fighter_id IN ({placeholders})",
                    (tournament_id, stage, *loser_ids),
                )
                or 0
            )
            losers_pool = player_losers + npc_losers
            self._conn.execute(
                f"UPDATE arena_bets SET settled=1 WHERE tournament_id=? AND stage=? AND fighter_id IN ({placeholders})",
                (tournament_id, stage, *loser_ids),
            )
            self._conn.execute(
                f"UPDATE arena_npc_bets SET settled=1 WHERE tournament_id=? AND stage=? AND fighter_id IN ({placeholders})",
                (tournament_id, stage, *loser_ids),
            )
            self._conn.execute(
                f"UPDATE arena_fighters SET stage='out', result='lost' WHERE id IN ({placeholders})", loser_ids
            )
        rate = win_odds
        player_bets = self._all(
            "SELECT * FROM arena_bets WHERE tournament_id=? AND stage=? AND fighter_id=? AND settled=0",
            (tournament_id, stage, winner["id"]),
        )
        # 案B(#27): 勝てば元本は必ず戻す。利益は負けた側の賭け金と、足りない分だけ帝愛の手元現金から上限つきで補う
        # 補填と銀行の収入には時刻が要る。時刻が渡されないとき(古い呼び出し)は補填しない
        state = self._bank.bank_sync_for(guild_id, now) if now is not None else None
        cap = min(arena_mod.MATCH_SUBSIDY_MAX, max(0, int(state["cash"]))) if state is not None else 0
        pays, subsidy, leftover = arena_mod.settle_payouts([b["amount"] for b in player_bets], rate, losers_pool, cap)
        if subsidy and state is not None:
            self._bank.bank_set_for(guild_id, state["cash"] - subsidy, state["loaned"])
        if (
            leftover and now is not None
        ):  # 使われなかった負け分は、カジノの負け分と同じく一部を帝愛銀行の収入にする(残りは消える)
            self._bank.bank_income_for(guild_id, int(leftover * teiai.LOSS_SHARE), now)
        for b, pay in zip(player_bets, pays, strict=True):
            if pay:
                self._account.apply(guild_id, b["user_id"], pay, "闘技場:勝利の配当", income=max(0, pay - b["amount"]))
            self._conn.execute("UPDATE arena_bets SET payout=?, settled=1 WHERE id=?", (pay, b["id"]))
        self._conn.execute(
            "UPDATE arena_npc_bets SET settled=1 WHERE tournament_id=? AND stage=? AND fighter_id=? AND settled=0",
            (tournament_id, stage, winner["id"]),
        )
        self._conn.execute("UPDATE arena_fighters SET result='won' WHERE id=?", (winner["id"],))

    def arena_run_semis(
        self, tournament_id: int, rng: random.Random | None = None, now: float | None = None
    ) -> ArenaSemiResult | None:
        """準決勝の全組を同時に決着させ、決勝へ進む人（組の数ぶん）を決める。"""
        rng = rng or random.SystemRandom()
        with self._db.tx():
            gid = self._g()
            t = self._one("SELECT * FROM arena_tournaments WHERE id=?", (tournament_id,))
            if t is None or t["guild_id"] != gid or t["status"] != "semi_betting":
                return None
            fighters = self.arena_fighters(tournament_id)
            finalists = []
            groups: dict[int, ArenaGroupResult] = {}
            for group_no in range(1, arena_mod.semi_groups(t["format"]) + 1):
                group = [f for f in fighters if f["group_no"] == group_no]
                bet_totals = {f["name"]: f["bet_total"] for f in group}
                win_idx = arena_mod.simulate_match(group, bet_totals, rng)
                winner, losers = group[win_idx], [f for i, f in enumerate(group) if i != win_idx]
                win_odds = arena_mod.match_odds(group)[winner["name"]]
                self._arena_settle_group(gid, tournament_id, "semi", winner, losers, win_odds, now)
                finalists.append(winner)
                groups[group_no] = {"winner": winner["name"], "losers": [f["name"] for f in losers]}
            for w in finalists:
                self._conn.execute("UPDATE arena_fighters SET stage='final', result=NULL WHERE id=?", (w["id"],))
            self._conn.execute("UPDATE arena_tournaments SET status='final_betting' WHERE id=?", (tournament_id,))
            return {"groups": groups, "finalists": [w["name"] for w in finalists]}

    def arena_run_final(
        self, tournament_id: int, rng: random.Random | None = None, now: float | None = None
    ) -> ArenaFinalResult | None:
        """決勝を決着させ、優勝者を決める（準決勝の勝者全員で、同時に戦う）。"""
        rng = rng or random.SystemRandom()
        with self._db.tx():
            gid = self._g()
            t = self._one("SELECT * FROM arena_tournaments WHERE id=?", (tournament_id,))
            if t is None or t["guild_id"] != gid or t["status"] != "final_betting":
                return None
            finalists = [f for f in self.arena_fighters(tournament_id) if f["stage"] == "final"]
            if len(finalists) != arena_mod.semi_groups(t["format"]):
                return None
            bet_totals = {f["name"]: f["bet_total"] for f in finalists}
            win_idx = arena_mod.simulate_match(finalists, bet_totals, rng)
            champion = finalists[win_idx]
            losers = [f for i, f in enumerate(finalists) if i != win_idx]
            win_odds = arena_mod.match_odds(finalists, boost=arena_mod.FINAL_ODDS_BOOST)[champion["name"]]
            self._arena_settle_group(gid, tournament_id, "final", champion, losers, win_odds, now)
            self._conn.execute("UPDATE arena_tournaments SET status='done' WHERE id=?", (tournament_id,))
            return {"champion": champion["name"], "runner_up": [f["name"] for f in losers]}

    def arena_my_bets(self, tournament_id: int, user_id: int) -> list[ArenaBetRow]:
        with self._lock:
            return self._all(
                "SELECT b.*, f.name AS fighter_name FROM arena_bets b JOIN arena_fighters f ON f.id=b.fighter_id "
                "WHERE b.tournament_id=? AND b.user_id=? ORDER BY b.id",
                (tournament_id, user_id),
            )

    def arena_bets_breakdown(self, tournament_id: int) -> dict[int, list[ArenaBetRow]]:
        """戦士ごとの、誰がいくら賭けているか（大会を通じた累積）。{戦士ID: [{user_id, amount}]}（金額の多い順）。"""
        with self._lock:
            rows = self._all(
                "SELECT fighter_id, user_id, SUM(amount) AS amount FROM ("
                "SELECT fighter_id, user_id, amount FROM arena_bets WHERE tournament_id=? "
                "UNION ALL "
                "SELECT fighter_id, ? AS user_id, amount FROM arena_npc_bets WHERE tournament_id=?"
                ") GROUP BY fighter_id, user_id ORDER BY fighter_id, amount DESC",
                (tournament_id, arena_mod.NPC_SPECTATOR, tournament_id),
            )
        out: dict[int, list[Any]] = {}
        for r in rows:
            out.setdefault(r["fighter_id"], []).append({"user_id": r["user_id"], "amount": r["amount"]})
        return out
