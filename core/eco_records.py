"""遊びの記録（Economy に混ぜ込む部品）。連勝・デイリーミッション・週間ランキング。

表の定義は core/economy.py の _init_schema にある。
"""

import collections
import datetime
from typing import Optional

from core import history as history_mod
from core import missions as missions_mod
from core import shop as shop_mod
from core import weekly as weekly_mod
from core.eco_base import EcoBase


class RecordMixin(EcoBase):
    """遊びの記録（Economy に混ぜ込む部品）。連勝・デイリーミッション・週間ランキング。"""

    # ------------------------------------------------------------------ #
    # 連勝とデイリーミッション                                            #
    # ------------------------------------------------------------------ #
    def record_play(self, user_id: int, game: str, bet: int, net: int, day: datetime.date, tags=()) -> dict:
        """1回遊んだ結果を記録する。連勝を更新し、デイリーミッションを進めて、達成した分の報酬を渡す。

        {"streak", "new_best", "completed": [(ミッション, 報酬)], "bonus"} を返す。引き分け（損益0）は連勝を変えない。
        tags は、ゲームが付ける印（ブラックジャックのダブル勝ちなど）。
        """
        with self._lock, self._conn:
            self._ensure(user_id)
            g, d = self._g(), day.isoformat()
            row = self._conn.execute(
                "SELECT win_streak, best_streak, day_streak, streak_day FROM play_state WHERE guild_id=? AND user_id=?",
                (g, user_id),
            ).fetchone()
            streak, best = (row["win_streak"], row["best_streak"]) if row else (0, 0)
            day_streak = (
                row["day_streak"] if row and row["streak_day"] == d else 0
            )  # ミッション用の連勝は、日をまたいで持ち越さない
            if net > 0:
                streak += 1
            elif net < 0:
                streak = 0
            counts = bet >= missions_mod.MIN_BET  # 小さい賭けの量産でミッションを埋められないようにする
            if counts:
                day_streak = day_streak + 1 if net > 0 else (0 if net < 0 else day_streak)
            new_best = streak > best
            best = max(best, streak)
            self._conn.execute(
                "INSERT INTO play_state (guild_id, user_id, win_streak, best_streak, day_streak, streak_day, plays, wins, net_total) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET win_streak=excluded.win_streak, best_streak=excluded.best_streak, "
                "day_streak=excluded.day_streak, streak_day=excluded.streak_day, plays=plays+1, wins=wins+excluded.wins, "
                "net_total=net_total+excluded.net_total",
                (
                    g,
                    user_id,
                    streak,
                    best,
                    day_streak,
                    d if counts else (row["streak_day"] if row else None),
                    int(net > 0),
                    net,
                ),
            )
            self._conn.execute(
                "INSERT INTO weekly_stats (guild_id, week, user_id, net, plays) VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(guild_id, week, user_id) DO UPDATE SET net=net+excluded.net, plays=plays+1",
                (g, weekly_mod.week_id(day), user_id, net),
            )
            completed, bonus = [], 0
            missions = missions_mod.today_missions(day) if counts else []
            for m in missions:
                r = self._conn.execute(
                    "SELECT progress, done FROM missions WHERE guild_id=? AND user_id=? AND day=? AND mission_id=?",
                    (g, user_id, d, m.id),
                ).fetchone()
                if r and r["done"]:
                    continue
                progress = missions_mod.progress_after(m, r["progress"] if r else 0, game, bet, net, day_streak, tags)
                done = progress >= m.target
                self._conn.execute(
                    "INSERT INTO missions (guild_id, user_id, day, mission_id, progress, done) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, user_id, day, mission_id) DO UPDATE SET progress=excluded.progress, done=excluded.done",
                    (g, user_id, d, m.id, min(progress, m.target), int(done)),
                )
                if done:
                    self._apply(user_id, m.reward, f"ミッション報酬:{m.name}", income=m.reward)
                    completed.append((m, m.reward))
            if completed:
                n = self._conn.execute(
                    "SELECT COUNT(*) FROM missions WHERE guild_id=? AND user_id=? AND day=? AND done=1", (g, user_id, d)
                ).fetchone()[0]
                if n >= missions_mod.PER_DAY:
                    bonus = missions_mod.COMPLETE_BONUS
                    self._apply(user_id, bonus, "ミッション全達成ボーナス", income=bonus)
            return {"streak": streak, "new_best": new_best, "completed": completed, "bonus": bonus}

    def mission_status(self, user_id: int, day: datetime.date) -> list:
        """今日のミッションごとの (ミッション, 進み具合, 達成済みか)。口座は作らない。"""
        with self._lock:
            out = []
            for m in missions_mod.today_missions(day):
                r = self._conn.execute(
                    "SELECT progress, done FROM missions WHERE guild_id=? AND user_id=? AND day=? AND mission_id=?",
                    (self._g(), user_id, day.isoformat(), m.id),
                ).fetchone()
                out.append((m, r["progress"] if r else 0, bool(r and r["done"])))
            return out

    # ------------------------------------------------------------------ #
    # 週間ランキング（先週の稼ぎ1位に、限定称号）                           #
    # ------------------------------------------------------------------ #
    def weekly_top(self, week: str, limit: int = 10) -> list:
        """[(ユーザーID, 稼ぎ, ゲーム数), ...]。その週の稼ぎが多い順（同じなら、ゲーム数が少ない順）。"""
        with self._lock:
            return [
                (r["user_id"], r["net"], r["plays"])
                for r in self._conn.execute(
                    "SELECT user_id, net, plays FROM weekly_stats WHERE guild_id=? AND week=? ORDER BY net DESC, plays ASC, user_id LIMIT ?",
                    (self._g(), week, limit),
                )
            ]

    def weekly_rank(self, week: str, user_id: int) -> Optional[tuple]:
        """(順位, 稼ぎ, ゲーム数, 人数)。その週に遊んでいなければ None。"""
        rows = self.weekly_top(week, 10_000)
        for i, (uid, net, plays) in enumerate(rows):
            if uid == user_id:
                return i + 1, net, plays, len(rows)
        return None

    def weekly_last_announced(self) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT last_week FROM weekly_state WHERE guild_id=?", (self._g(),)).fetchone()
            return r["last_week"] if r else None

    def set_weekly_announced(self, week: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO weekly_state (guild_id, last_week) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET last_week=excluded.last_week",
                (self._g(), week),
            )

    def weekly_champion_count(self, user_id: int) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM weekly_champions WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()[0]

    def weekly_award(self, week: str) -> Optional[dict]:
        """その週の発表。王者（稼ぎがプラスで、ゲーム数が規定以上の1位）に限定称号を贈り、前の週の保持者からは外す。

        すでにその週以降を発表済みなら何もせず None。{"champion": (ユーザーID, 稼ぎ, ゲーム数) or None, "previous": [外れた人], "wins": 通算回数, "standings": 上位}
        """
        with self._lock, self._conn:
            last = self.weekly_last_announced()
            if last is not None and last >= week:
                return None
            g, king = self._g(), shop_mod.WEEKLY_KING
            previous = [
                r["user_id"]
                for r in self._conn.execute("SELECT user_id FROM inventory WHERE guild_id=? AND item_id=?", (g, king))
            ]
            self._conn.execute("DELETE FROM inventory WHERE guild_id=? AND item_id=?", (g, king))
            self._conn.execute(
                "UPDATE profile SET title=NULL WHERE guild_id=? AND title=?", (g, king)
            )  # 装備していた人は、外す
            top = self._conn.execute(
                "SELECT user_id, net, plays FROM weekly_stats WHERE guild_id=? AND week=? AND net>0 AND plays>=? "
                "ORDER BY net DESC, plays ASC, user_id LIMIT 1",
                (g, week, weekly_mod.MIN_PLAYS),
            ).fetchone()
            champion, wins = None, 0
            if top:
                champion = (top["user_id"], top["net"], top["plays"])
                self._conn.execute(
                    "INSERT OR REPLACE INTO weekly_champions (guild_id, week, user_id, net) VALUES (?, ?, ?, ?)",
                    (g, week, top["user_id"], top["net"]),
                )
                self._conn.execute(
                    "INSERT INTO inventory (guild_id, user_id, item_id, qty) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(guild_id, user_id, item_id) DO UPDATE SET qty=1",
                    (g, top["user_id"], king),
                )
                wins = self.weekly_champion_count(top["user_id"])
            self.set_weekly_announced(week)
            return {
                "champion": champion,
                "previous": [u for u in previous if not champion or u != champion[0]],
                "wins": wins,
                "standings": self.weekly_top(week, weekly_mod.ANNOUNCE_TOP),
            }

    def backfill_weekly(self, utc_offset_hours: float = 9) -> int:
        """この機能の前に遊んだ分を、台帳から週間の記録に入れる（初回だけ）。入れたゲーム数を返す。"""
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM weekly_state WHERE guild_id=0").fetchone():
                return 0
            tz = datetime.timezone(datetime.timedelta(hours=float(utc_offset_hours)))
            by_user = collections.defaultdict(list)
            for r in self._conn.execute(
                "SELECT guild_id, user_id, delta, balance_after, reason, ts FROM ledger ORDER BY id"
            ):
                by_user[(r["guild_id"], r["user_id"])].append(dict(r))
            count = 0
            for (g, uid), rows in by_user.items():
                if g <= 0:
                    continue
                for ts, _game, net in history_mod.game_results(rows):
                    week = weekly_mod.week_id(datetime.datetime.fromtimestamp(ts, tz).date())
                    self._conn.execute(
                        "INSERT INTO weekly_stats (guild_id, week, user_id, net, plays) VALUES (?, ?, ?, ?, 1) "
                        "ON CONFLICT(guild_id, week, user_id) DO UPDATE SET net=net+excluded.net, plays=plays+1",
                        (g, week, uid, net),
                    )
                    count += 1
            self._conn.execute("INSERT INTO weekly_state (guild_id, last_week) VALUES (0, 'backfilled')")
            return count

    def play_summary(self, user_id: int) -> dict:
        """ゲームの成績（遊んだ回数・勝った回数・損益・最大連勝）。読み取り専用で、口座は作らない。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT plays, wins, net_total, best_streak FROM play_state WHERE guild_id=? AND user_id=?",
                (self._g(), user_id),
            ).fetchone()
            return dict(r) if r else {"plays": 0, "wins": 0, "net_total": 0, "best_streak": 0}

    def win_streak(self, user_id: int) -> tuple:
        """(いまの連勝, 自己ベスト)。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT win_streak, best_streak FROM play_state WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            return (r["win_streak"], r["best_streak"]) if r else (0, 0)
