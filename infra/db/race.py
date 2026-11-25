"""競馬のレース運営・出走・オッズ・馬券・発走精算。"""

import datetime
import random
from collections import Counter
from typing import Any

from domain import horse as horse_mod
from domain import teiai
from domain.errors import Failure, Outcome, fail
from domain.ports.bridges import IBankCashFlow
from domain.ports.contracts import RaceBetResult, RaceBetRow, RaceCloseResult, RaceEntryRow, RaceRow, RaceRunResult
from domain.ports.repos import IRaceRepo
from infra.db.account import AccountCore
from infra.db.base import EcoDb, ScopedRepo
from infra.db.discord_posts import init_discord_post, set_discord_message
from infra.db.horse import HorseRepo
from infra.db.relics import RelicRepo

_SELECT_RACE = (
    "SELECT r.id, r.guild_id, r.kind, r.start_at, r.entry_close, r.bet_close, "
    "r.status, r.distance, r.going, "
    "COALESCE(p.channel_id, 0) AS channel_id, COALESCE(p.message_id, 0) AS message_id, r.created "
    "FROM horse_races r "
    "LEFT JOIN discord_posts p ON p.entity_type = 'horse_race' AND p.entity_id = r.id"
)


def race_slots(now: float, tz_hours: float, days: int = 2) -> list[float]:
    """now より後の、定時レースの発走時刻（早い順）。"""
    tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
    today = datetime.datetime.fromtimestamp(now, tz).date()
    out = []
    for d in range(days + 1):
        for hour, minute in horse_mod.schedule_for(today + datetime.timedelta(days=d)):
            t = datetime.datetime.combine(
                today + datetime.timedelta(days=d), datetime.time(hour, minute), tzinfo=tz
            ).timestamp()
            if t > now:
                out.append(t)
    return sorted(out)


def _parse_odds_key(kind: str, key: str) -> tuple[int, ...]:
    sep = ">" if kind == "exacta" else "-"
    lanes = [int(x) for x in key.split(sep)]
    return tuple(lanes) if kind == "exacta" else tuple(sorted(lanes))


class RaceRepo(ScopedRepo, IRaceRepo):
    """レースのスケジュール・出走登録・オッズ設定・馬券購入・レース実行と精算。"""

    def __init__(
        self,
        db: EcoDb,
        account: AccountCore,
        horses: HorseRepo,
        relics: RelicRepo,
        bank: IBankCashFlow,
        guild_id: int = 0,
        strict: bool = False,
    ) -> None:
        super().__init__(db, guild_id, strict)
        self._account = account
        self._horses = horses
        self._relics = relics
        self._bank = bank

    def race_slots(self, now: float, tz_hours: float, days: int = 2) -> list[float]:
        return race_slots(now, tz_hours, days)

    def race_get(self, race_id: int) -> RaceRow | None:
        with self._lock:
            return self._one(f"{_SELECT_RACE} WHERE r.id=? AND r.guild_id=?", (race_id, self._g()))

    def race_number(self, race_id: int) -> int:
        """このサーバーでの、第何競走か。"""
        with self._lock:
            return self._scalar("SELECT COUNT(*) FROM horse_races WHERE guild_id=? AND id<=?", (self._g(), race_id))

    def race_open(self) -> list[RaceRow]:
        """まだ終わっていないレース（申し込み中・発売中）。発走の早い順。"""
        with self._lock:
            return self._all(
                f"{_SELECT_RACE} WHERE r.guild_id=? AND r.status IN ('entry','betting') ORDER BY r.start_at",
                (self._g(),),
            )

    def race_ensure_scheduled(self, now: float, tz_hours: float, channel_id: int = 0) -> int | None:
        """次の定時レースが、申し込みを始める時刻になっていて、まだ無ければ作る（申し込み受付を始める）。作ったレースの番号を返す。"""
        with self._db.tx():
            gid = self._g()
            start = race_slots(now, tz_hours)[0]
            if now < start - horse_mod.ENTRY_OPEN_MIN * 60:
                return None
            if self._one("SELECT 1 FROM horse_races WHERE guild_id=? AND kind='regular' AND start_at=?", (gid, start)):
                return None
            cur = self._conn.execute(
                "INSERT INTO horse_races (guild_id, kind, start_at, entry_close, bet_close, status, created) "
                "VALUES (?, 'regular', ?, ?, ?, 'entry', ?)",
                (
                    gid,
                    start,
                    start - horse_mod.ENTRY_LEAD_MIN * 60,
                    start - horse_mod.BET_LEAD_SEC,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            init_discord_post(self._conn, "horse_race", cur.lastrowid, channel_id)
            return cur.lastrowid

    def race_create_special(self, now: float, entry_min: int, bet_min: int, channel_id: int = 0) -> int:
        """臨時レースを作る（管理者用）。申し込みを entry_min 分、そのあと馬券を bet_min 分売って、2分後に発走。"""
        with self._db.tx():
            entry_close = now + entry_min * 60
            bet_close = entry_close + bet_min * 60
            cur = self._conn.execute(
                "INSERT INTO horse_races (guild_id, kind, start_at, entry_close, bet_close, status, created) "
                "VALUES (?, 'special', ?, ?, ?, 'entry', ?)",
                (self._g(), bet_close + horse_mod.BET_LEAD_SEC, entry_close, bet_close, now),
            )
            assert cur.lastrowid is not None
            init_discord_post(self._conn, "horse_race", cur.lastrowid, channel_id)
            return cur.lastrowid

    def race_set_message(self, race_id: int, channel_id: int, message_id: int) -> None:
        with self._db.tx():
            set_discord_message(self._conn, "horse_race", race_id, message_id, channel_id)

    def _registered_entries(self, race_id: int) -> list[RaceEntryRow]:
        return [
            {
                "id": r["horse_id"],
                "race_id": r["race_id"],
                "lane": -1,
                "horse_id": r["horse_id"],
                "owner_id": r["owner_id"],
                "name": r["name"],
                "coat": r["coat"],
                "silks": r["silks"],
                "speed": 50,
                "stamina": 50,
                "power": 50,
                "guts": 50,
                "style": None,
                "form": 0,
                "races": 0,
                "wins": 0,
                "places": 0,
                "odds_win": 0.0,
                "odds_place": 0.0,
                "finish": 0,
            }
            for r in self._all(
                "SELECT race_id, horse_id, owner_id, name, coat, silks FROM horse_race_registrations "
                "WHERE race_id=? ORDER BY horse_id",
                (race_id,),
            )
        ]

    def race_entries(self, race_id: int, registered_only: bool = False) -> list[RaceEntryRow]:
        with self._lock:
            if registered_only:
                return self._registered_entries(race_id)
            rows: list[RaceEntryRow] = self._all(
                "SELECT id, race_id, lane, COALESCE(horse_id, 0) AS horse_id, COALESCE(owner_id, 0) AS owner_id, "
                "name, coat, silks, speed, stamina, power, guts, style, form, races, wins, places, "
                "odds_win, odds_place, finish FROM horse_entries WHERE race_id=? ORDER BY lane, id",
                (race_id,),
            )
            return rows if rows else self._registered_entries(race_id)

    def race_register(self, user_id: int, horse_id: int, race_id: int, now: float) -> Outcome[str]:
        """馬を出走させる（出走料がかかる）。(成功か, 理由)。1人1レースに1頭まで。"""
        with self._db.tx():
            gid = self._g()
            race = self.race_get(race_id)
            if race is None or race["status"] != "entry" or now >= race["entry_close"]:
                return False, fail("race_entry_closed")
            h = self._horses.horse_get_for(gid, horse_id)
            if h is None or h["owner_id"] != user_id:
                return False, fail("horse_select_own")
            if h["races"] >= horse_mod.MAX_RACES:
                return False, fail("horse_retired_by_age", max_races=horse_mod.MAX_RACES)
            rows = self.race_entries(race_id, registered_only=True)
            if any(e["owner_id"] == user_id for e in rows):
                return False, fail("race_one_horse_per_owner")
            if len(rows) >= horse_mod.FIELD_SIZE:
                return False, fail("race_field_full")
            if any(e["name"] == h["name"] for e in rows):
                return False, fail("race_duplicate_name")
            if race["kind"] != "special" and self._horses.horse_is_in_active_race_for(horse_id, regular_only=True):
                return False, fail("race_horse_busy")
            fee = horse_mod.ENTRY_FEE
            self._account.ensure(gid, user_id)
            bal = self._account.raw_balance(gid, user_id)
            if bal < fee:
                return False, fail("race_entry_fee_short", fee=fee, balance=bal)
            self._account.apply(gid, user_id, -fee, f"競馬:出走料（{h['name']}）")
            self._conn.execute(
                "INSERT INTO horse_race_registrations (race_id, horse_id, owner_id, name, coat, silks) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (race_id, horse_id, user_id, h["name"], h["coat"], h["silks"]),
            )
            return True, ""

    def race_withdraw(self, user_id: int, horse_id: int, race_id: int, now: float) -> Outcome[str]:
        """締切前なら、申し込みを取り下げて、出走料を返す。"""
        with self._db.tx():
            gid = self._g()
            race = self.race_get(race_id)
            if race is None or race["status"] != "entry" or now >= race["entry_close"]:
                return False, fail("race_entry_closed")
            cur = self._conn.execute(
                "DELETE FROM horse_race_registrations WHERE race_id=? AND horse_id=? AND owner_id=?",
                (race_id, horse_id, user_id),
            )
            if not cur.rowcount:
                return False, fail("race_not_entered")
            self._account.apply(gid, user_id, horse_mod.ENTRY_FEE, "競馬:出走料の返金")
            return True, ""

    def race_close_entries(self, race_id: int, rng: random.Random | None = None) -> RaceCloseResult | None:
        """申し込みを締め切って、NPC で埋めた出走馬と、コースを決める（発売中にする）。"""
        rng = rng or random.SystemRandom()
        with self._db.tx():
            gid = self._g()
            race = self.race_get(race_id)
            if race is None or race["status"] != "entry":
                return None
            registered = self.race_entries(race_id, registered_only=True)
            rows = []
            for e in registered:
                h = self._horses.horse_get_for(gid, e["horse_id"]) or e
                rows.append(
                    (e["horse_id"], e["owner_id"], h["name"], h["coat"], h["silks"])
                    + tuple(h[k] for k in horse_mod.STAT_KEYS)
                    + (h.get("races", 0), h.get("wins", 0), h.get("places", 0))
                    + (h.get("style"),)
                )
            self._conn.execute("DELETE FROM horse_race_registrations WHERE race_id=?", (race_id,))
            self._conn.execute("DELETE FROM horse_entries WHERE race_id=?", (race_id,))
            taken = {r[2] for r in rows}
            pool = [n for n in horse_mod.NPC_NAMES if n not in taken]
            rng.shuffle(pool)
            used = Counter(r[4] for r in rows)
            while len(rows) < horse_mod.FIELD_SIZE:
                s = horse_mod.random_stats(rng)
                least = min(used[c] for c in horse_mod.SILKS)
                silks = rng.choice([c for c in horse_mod.SILKS if used[c] == least])
                used[silks] += 1
                rows.append(
                    (
                        0,
                        0,
                        pool.pop() if pool else f"帝愛{len(rows)}号",
                        rng.choice(horse_mod.COATS),
                        silks,
                    )
                    + tuple(s[k] for k in horse_mod.STAT_KEYS)
                    + horse_mod.npc_record(rng)
                    + (None,)
                )
            rng.shuffle(rows)
            runners = []
            for lane, row in enumerate(rows, start=1):
                form = horse_mod.random_form(rng)
                self._conn.execute(
                    "INSERT INTO horse_entries (race_id, lane, horse_id, owner_id, name, coat, silks, "
                    "speed, stamina, power, guts, races, wins, places, style, form) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (race_id, lane, row[0] or None, row[1] or None, *row[2:], form),
                )
                runners.append(
                    horse_mod.Runner(
                        lane, row[2], row[5], row[6], row[7], row[8], form, row[9], row[10], row[11], row[12]
                    )
                )
            track = horse_mod.random_track(rng)
            self._conn.execute(
                "UPDATE horse_races SET status='betting', distance=?, going=? WHERE id=?",
                (track.distance, track.going, race_id),
            )
            return {"runners": runners, "track": track}

    def _insert_combo_odds(self, race_id: int, odds: dict[str, dict[str, float]]) -> None:
        combo2: list[tuple[int, str, int, int, float]] = []
        trio: list[tuple[int, int, int, int, float]] = []
        for kind in horse_mod.COMBO_SIZE:
            for key, o in odds.get(kind, {}).items():
                lanes = _parse_odds_key(kind, key)
                if len(lanes) == 2:
                    combo2.append((race_id, kind, lanes[0], lanes[1], o))
                elif len(lanes) == 3:
                    trio.append((race_id, lanes[0], lanes[1], lanes[2], o))
        if combo2:
            self._conn.executemany(
                "INSERT INTO horse_combo2_odds (race_id, kind, lane1, lane2, odds) VALUES (?, ?, ?, ?, ?)",
                combo2,
            )
        if trio:
            self._conn.executemany(
                "INSERT INTO horse_trio_odds (race_id, lane1, lane2, lane3, odds) VALUES (?, ?, ?, ?, ?)",
                trio,
            )

    def race_set_odds(self, race_id: int, odds: dict[str, dict[str, float]] | list[tuple[float, float]]) -> None:
        """オッズを書く。"""
        if not isinstance(odds, dict):
            odds = {
                "win": {str(i): w for i, (w, _) in enumerate(odds, start=1)},
                "place": {str(i): p for i, (_, p) in enumerate(odds, start=1)},
            }
        with self._db.tx():
            for lane in odds["win"]:
                self._conn.execute(
                    "UPDATE horse_entries SET odds_win=?, odds_place=? WHERE race_id=? AND lane=?",
                    (odds["win"][lane], odds["place"][lane], race_id, int(lane)),
                )
            self._conn.execute("DELETE FROM horse_combo2_odds WHERE race_id=?", (race_id,))
            self._conn.execute("DELETE FROM horse_trio_odds WHERE race_id=?", (race_id,))
            self._insert_combo_odds(race_id, odds)

    def race_odds(self, race_id: int, kind: str, lanes: list[int]) -> float | None:
        """その馬券のオッズ（なければ None）。lanes は、選んだ枠の並び（単勝・複勝は1つ）。"""
        with self._lock:
            match kind:
                case "win" | "place":
                    r = self._one(
                        "SELECT odds_win, odds_place FROM horse_entries WHERE race_id=? AND lane=?",
                        (race_id, lanes[0]),
                    )
                    if r is None:
                        return None
                    value = r["odds_win"] if kind == "win" else r["odds_place"]
                    return value if value else None
                case "exacta":
                    v = self._scalar(
                        "SELECT odds FROM horse_combo2_odds WHERE race_id=? AND kind='exacta' AND lane1=? AND lane2=?",
                        (race_id, lanes[0], lanes[1]),
                    )
                    return v if v is not None else None
                case "quinella" | "wide":
                    a, b = sorted(lanes)
                    v = self._scalar(
                        "SELECT odds FROM horse_combo2_odds WHERE race_id=? AND kind=? AND lane1=? AND lane2=?",
                        (race_id, kind, a, b),
                    )
                    return v if v is not None else None
                case "trio":
                    a, b, c = sorted(lanes)
                    v = self._scalar(
                        "SELECT odds FROM horse_trio_odds WHERE race_id=? AND lane1=? AND lane2=? AND lane3=?",
                        (race_id, a, b, c),
                    )
                    return v if v is not None else None
                case _:
                    return None

    def _bet_pick_lanes(self, bet_id: int) -> list[int]:
        return [
            r["lane"]
            for r in self._all("SELECT lane FROM horse_bet_picks WHERE bet_id=? ORDER BY position", (bet_id,))
        ]

    def _hydrate_bet(self, raw: dict[str, Any]) -> RaceBetRow:
        lanes = self._bet_pick_lanes(raw["id"])
        picks = horse_mod.combo_key(raw["kind"], lanes) if raw["kind"] in horse_mod.COMBO_SIZE else ""
        return {
            "id": raw["id"],
            "guild_id": raw["guild_id"],
            "race_id": raw["race_id"],
            "user_id": raw["user_id"],
            "kind": raw["kind"],
            "lane": lanes[0] if lanes else 0,
            "picks": picks,
            "stake": raw["stake"],
            "odds": raw["odds"],
            "payout": raw["payout"],
            "settled": raw["settled"],
            "created": raw["created"],
        }

    def race_bets(self, user_id: int, race_id: int) -> list[RaceBetRow]:
        with self._lock:
            rows = self._all("SELECT * FROM horse_bets WHERE race_id=? AND user_id=? ORDER BY id", (race_id, user_id))
            return [self._hydrate_bet(r) for r in rows]

    def race_bet(
        self, user_id: int, race_id: int, kind: str, lane: int | list[int], stake: int, now: float
    ) -> Outcome[RaceBetResult]:
        """馬券を買う。賭けた時点のオッズで確定する。"""
        if kind not in horse_mod.BET_KINDS:
            return False, fail("race_select_bet_kind")
        lanes = list(lane) if isinstance(lane, (list, tuple)) else [lane]
        need = horse_mod.COMBO_SIZE.get(kind, 1)
        if len(lanes) != need or len(set(lanes)) != need:
            return False, fail("race_select_distinct_horses", kind=kind, need=need)
        if not horse_mod.MIN_STAKE <= stake <= horse_mod.MAX_STAKE:
            return False, fail("race_stake_range", min_stake=horse_mod.MIN_STAKE, max_stake=horse_mod.MAX_STAKE)
        with self._db.tx():
            gid = self._g()
            race = self.race_get(race_id)
            if race is None or race["status"] != "betting":
                return False, fail("race_bet_unavailable")
            if now >= race["bet_close"]:
                return False, fail("race_bet_closed")
            names = []
            for x in lanes:
                entry = self._one("SELECT * FROM horse_entries WHERE race_id=? AND lane=?", (race_id, x))
                if entry is None:
                    return False, fail("race_lane_not_running")
                names.append(entry["name"])
            odds = self.race_odds(race_id, kind, lanes)
            if odds is None:
                return False, fail("race_odds_pending")
            self._account.ensure(gid, user_id)
            spent = self._scalar(
                "SELECT COALESCE(SUM(stake), 0) FROM horse_bets WHERE race_id=? AND user_id=?", (race_id, user_id)
            )
            if spent + stake > horse_mod.MAX_RACE_STAKE:
                return False, fail("race_max_stake", max_race_stake=horse_mod.MAX_RACE_STAKE)
            bal = self._account.raw_balance(gid, user_id)
            if bal < stake:
                return False, fail("insufficient_balance", balance=bal)
            label = "・".join(names)
            self._account.apply(gid, user_id, -stake, f"競馬:馬券（{horse_mod.BET_KINDS[kind]}・{label}）")
            self._account.record_wager(gid, user_id, stake)
            cur = self._conn.execute(
                "INSERT INTO horse_bets (guild_id, race_id, user_id, kind, stake, odds, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (gid, race_id, user_id, kind, stake, odds, now),
            )
            bet_id = cur.lastrowid
            assert bet_id is not None
            self._conn.executemany(
                "INSERT INTO horse_bet_picks (bet_id, position, lane) VALUES (?, ?, ?)",
                [(bet_id, pos, ln) for pos, ln in enumerate(lanes, start=1)],
            )
            result: RaceBetResult = {
                "id": bet_id,
                "kind": kind,
                "lane": lanes[0],
                "lanes": lanes,
                "stake": stake,
                "odds": odds,
                "name": label,
            }
            return True, result

    def race_bet_many(
        self, user_id: int, race_id: int, kind: str, lanes: list[int], stake: int, now: float
    ) -> Outcome[list[RaceBetResult]]:
        """単勝・複勝を、何頭かまとめて買う。"""
        if kind not in horse_mod.MULTI_KINDS:
            return False, fail("race_multi_only")
        lanes = list(dict.fromkeys(lanes))
        if not lanes:
            return False, fail("race_select_horses")
        if not horse_mod.MIN_STAKE <= stake <= horse_mod.MAX_STAKE:
            return False, fail("race_stake_range", min_stake=horse_mod.MIN_STAKE, max_stake=horse_mod.MAX_STAKE)
        with self._db.tx():
            gid = self._g()
            self._account.ensure(gid, user_id)
            spent = self._scalar(
                "SELECT COALESCE(SUM(stake), 0) FROM horse_bets WHERE race_id=? AND user_id=?", (race_id, user_id)
            )
            total = stake * len(lanes)
            if spent + total > horse_mod.MAX_RACE_STAKE:
                return False, fail(
                    "race_max_stake_many",
                    max_race_stake=horse_mod.MAX_RACE_STAKE,
                    total=total,
                    spent=spent,
                )
            bal = self._account.raw_balance(gid, user_id)
            if bal < total:
                return False, fail("race_insufficient_balance_many", total=total, balance=bal)
            bought = []
            for lane in lanes:
                ok, result = self.race_bet(user_id, race_id, kind, lane, stake, now)
                if ok:
                    bought.append(result)
                    continue
                if bought:
                    return False, fail("race_partial_bought", inner=result, bought=len(bought))
                assert isinstance(result, Failure)
                return False, result
            return True, bought

    def _bet_hits(self, kind: str, lanes: list[int], order_lanes: list[int]) -> bool:
        if kind == "win":
            return bool(lanes) and lanes[0] == order_lanes[0]
        if kind == "place":
            return bool(lanes) and lanes[0] in set(order_lanes[:3])
        return horse_mod.combo_hit(kind, lanes, order_lanes)

    def race_run(self, race_id: int, tz_hours: float, rng: random.Random | None = None) -> RaceRunResult | None:
        """レースを走らせて、結果を決め、賞金と払い戻しを精算する。すでに終わっていれば None。"""
        rng = rng or random.SystemRandom()
        with self._db.tx():
            gid = self._g()
            race = self.race_get(race_id)
            if race is None or race["status"] != "betting":
                return None
            self._conn.execute("UPDATE horse_races SET status='done' WHERE id=?", (race_id,))
            is_major = horse_mod.is_major_race(race, tz_hours)
            prizes_table = horse_mod.MAJOR_PRIZES if is_major else horse_mod.PRIZES
            entries = self.race_entries(race_id)
            styles = [e["style"] or horse_mod.style_of({k: e[k] for k in horse_mod.STAT_KEYS}) for e in entries]
            runners = [
                horse_mod.Runner(
                    e["lane"], e["name"], e["speed"], e["stamina"], e["power"], e["guts"], e["form"], style=s
                )
                for e, s in zip(entries, styles, strict=True)
            ]
            track = horse_mod.Track(race["distance"], race["going"])
            sim = horse_mod.simulate(runners, track, rng, keep_frames=True)
            order = sim["order"]
            real_entries = sum(1 for e in entries if e["owner_id"])
            placed_total = sum(
                prizes_table[p - 1]
                for p, idx in enumerate(order, start=1)
                if p <= len(prizes_table) and entries[idx]["owner_id"]
            )
            topup = 0
            shortfall = max(0, placed_total - real_entries * horse_mod.ENTRY_FEE) if is_major else 0
            if shortfall:
                state = self._bank.bank_sync_for(gid, self._now())
                topup = min(shortfall, int(state["cash"]), horse_mod.MAJOR_HOUSE_TOPUP)
                if topup:
                    self._bank.bank_set_for(gid, state["cash"] - topup, state["loaned"])
            scale = horse_mod.prize_pool_scale(real_entries, placed_total, topup)
            prizes: dict[int, int] = {}
            retired = []
            buffs_by_owner: dict[int, dict[str, float]] = {}
            for place, idx in enumerate(order, start=1):
                e = entries[idx]
                e["finish"] = place
                self._conn.execute("UPDATE horse_entries SET finish=? WHERE id=?", (place, e["id"]))
                prize = round((prizes_table[place - 1] if place <= len(prizes_table) else 0) * scale)
                if not e["owner_id"]:
                    continue
                if prize and e["owner_id"] not in buffs_by_owner:
                    buffs_by_owner[e["owner_id"]] = self._relics.relic_buffs_for(gid, e["owner_id"])
                if prize:
                    prize = round(prize * (1 + buffs_by_owner[e["owner_id"]].get("prize", 0.0)))
                    skim = round(prize * teiai.HORSE_PRIZE_SKIM_RATE)
                    net = prize - skim
                    self._account.apply(gid, e["owner_id"], net, f"競馬賞金:{e['name']}（{place}着）", income=net)
                    prizes[e["owner_id"]] = prizes.get(e["owner_id"], 0) + net
                    prize = net
                self._conn.execute(
                    "UPDATE horses SET races=races+1, wins=wins+?, places=places+?, earnings=earnings+? WHERE id=?",
                    (1 if place == 1 else 0, 1 if place <= 3 else 0, prize, e["horse_id"]),
                )
                horse = self._horses.horse_get_for(gid, e["horse_id"])
                if horse is not None and horse["races"] >= horse_mod.MAX_RACES:
                    value = horse_mod.retire_value(horse["races"], horse["wins"], horse["places"])
                    self._account.ensure(gid, e["owner_id"])
                    if value:
                        self._account.apply(
                            gid,
                            e["owner_id"],
                            value,
                            f"競馬:定年引退{'金' if value > 0 else '費用'}（{horse['name']}）",
                            income=max(0, value),
                        )
                    self._horses.horse_retire_for(horse["id"])
                    retired.append({**horse, "value": value})
            order_lanes = [entries[i]["lane"] for i in order]
            bets, lost = [], 0
            for raw_bet in self._all("SELECT * FROM horse_bets WHERE race_id=? AND settled=0 ORDER BY id", (race_id,)):
                lanes = self._bet_pick_lanes(raw_bet["id"])
                hit = self._bet_hits(raw_bet["kind"], lanes, order_lanes)
                pay = horse_mod.payout(raw_bet["stake"], raw_bet["odds"]) if hit else 0
                if pay:
                    self._account.apply(
                        gid, raw_bet["user_id"], pay, "競馬:払い戻し", income=max(0, pay - raw_bet["stake"])
                    )
                else:
                    lost += raw_bet["stake"]
                self._conn.execute("UPDATE horse_bets SET payout=?, settled=1 WHERE id=?", (pay, raw_bet["id"]))
                self._account.record_game_result(gid, raw_bet["user_id"], pay, pay - raw_bet["stake"])
                hydrated = self._hydrate_bet({**raw_bet, "payout": pay, "settled": 1})
                bets.append(hydrated)
            self._bank.bank_income_for(gid, int(lost * teiai.LOSS_SHARE), self._now())
            self._conn.execute("DELETE FROM horse_combo2_odds WHERE race_id=?", (race_id,))
            self._conn.execute("DELETE FROM horse_trio_odds WHERE race_id=?", (race_id,))
            return {
                "race": race,
                "entries": entries,
                "order": order,
                "frames": sim["frames"],
                "finish": sim["finish"],
                "bets": bets,
                "prizes": prizes,
                "track": track,
                "retired": retired,
                "is_major": is_major,
                "styles": dict(zip((e["lane"] for e in entries), styles, strict=True)),
            }
