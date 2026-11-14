"""競馬のレース運営・出走・オッズ・馬券・発走精算（Economy に混ぜ込む部品）。"""

import datetime
import random
import time
from collections import Counter
from typing import Optional

from domain import horse as horse_mod
from domain import teiai
from domain.errors import fail
from infra.db.base import EcoBase


def race_slots(now: float, tz_hours: float, days: int = 2) -> list:
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


class RaceMixin(EcoBase):
    """レースのスケジュール・出走登録・オッズ設定・馬券購入・レース実行と精算。"""

    race_slots = staticmethod(race_slots)

    def race_get(self, race_id: int) -> Optional[dict]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM horse_races WHERE id=? AND guild_id=?", (race_id, self._g())
            ).fetchone()
            return dict(r) if r else None

    def race_number(self, race_id: int) -> int:
        """このサーバーでの、第何競走か。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM horse_races WHERE guild_id=? AND id<=?", (self._g(), race_id)
            ).fetchone()[0]

    def race_open(self) -> list:
        """まだ終わっていないレース（申し込み中・発売中）。発走の早い順。"""
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM horse_races WHERE guild_id=? AND status IN ('entry','betting') ORDER BY start_at",
                    (self._g(),),
                )
            ]

    def race_ensure_scheduled(self, now: float, tz_hours: float, channel_id: int = 0) -> Optional[int]:
        """次の定時レースが、申し込みを始める時刻になっていて、まだ無ければ作る（申し込み受付を始める）。作ったレースの番号を返す。"""
        with self._lock, self._conn:
            start = race_slots(now, tz_hours)[0]
            if now < start - horse_mod.ENTRY_OPEN_MIN * 60:  # まだ、申し込みを始める時刻ではない
                return None
            if self._conn.execute(
                "SELECT 1 FROM horse_races WHERE guild_id=? AND kind='regular' AND start_at=?", (self._g(), start)
            ).fetchone():
                return None
            cur = self._conn.execute(
                "INSERT INTO horse_races (guild_id, kind, start_at, entry_close, bet_close, status, channel_id, created) "
                "VALUES (?, 'regular', ?, ?, ?, 'entry', ?, ?)",
                (
                    self._g(),
                    start,
                    start - horse_mod.ENTRY_LEAD_MIN * 60,
                    start - horse_mod.BET_LEAD_SEC,
                    channel_id,
                    now,
                ),
            )
            return cur.lastrowid

    def race_create_special(self, now: float, entry_min: int, bet_min: int, channel_id: int = 0) -> int:
        """臨時レースを作る（管理者用）。申し込みを entry_min 分、そのあと馬券を bet_min 分売って、2分後に発走。"""
        with self._lock, self._conn:
            entry_close = now + entry_min * 60
            bet_close = entry_close + bet_min * 60
            cur = self._conn.execute(
                "INSERT INTO horse_races (guild_id, kind, start_at, entry_close, bet_close, status, channel_id, created) "
                "VALUES (?, 'special', ?, ?, ?, 'entry', ?, ?)",
                (self._g(), bet_close + horse_mod.BET_LEAD_SEC, entry_close, bet_close, channel_id, now),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def race_set_message(self, race_id: int, channel_id: int, message_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE horse_races SET channel_id=?, message_id=? WHERE id=?", (channel_id, message_id, race_id)
            )

    def race_entries(self, race_id: int, registered_only: bool = False) -> list:
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM horse_entries WHERE race_id=?"
                    + (" AND lane<0" if registered_only else "")
                    + " ORDER BY CASE WHEN lane<0 THEN 99 ELSE lane END, id",
                    (race_id,),
                )
            ]

    def race_register(self, user_id: int, horse_id: int, race_id: int, now: float) -> tuple:
        """馬を出走させる（出走料がかかる）。(成功か, 理由)。1人1レースに1頭まで。"""
        with self._lock, self._conn:
            race = self.race_get(race_id)
            if race is None or race["status"] != "entry" or now >= race["entry_close"]:
                return False, fail("race_entry_closed")
            h = self.horse_get(horse_id)
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
            # 出走予定の馬は、ほかのレースに出せない。ただし、臨時レースは別（出走予定の馬も出せる。臨時に出ていても、定時に出せる）
            busy = (
                None
                if race["kind"] == "special"
                else self._conn.execute(
                    "SELECT 1 FROM horse_entries e JOIN horse_races r ON r.id=e.race_id "
                    "WHERE e.horse_id=? AND r.status IN ('entry','betting') AND r.kind='regular'",
                    (horse_id,),
                ).fetchone()
            )
            if busy:
                return False, fail("race_horse_busy")
            fee = horse_mod.ENTRY_FEE
            self._ensure(user_id)
            if self._balance(user_id) < fee:
                return False, fail("race_entry_fee_short", fee=fee, balance=self._balance(user_id))
            self._apply(user_id, -fee, f"競馬:出走料（{h['name']}）")
            self._conn.execute(
                "INSERT INTO horse_entries (race_id, lane, horse_id, owner_id, name, coat, silks) "
                "VALUES (?, -1, ?, ?, ?, ?, ?)",
                (race_id, horse_id, user_id, h["name"], h["coat"], h["silks"]),
            )
            return True, ""

    def race_withdraw(self, user_id: int, horse_id: int, race_id: int, now: float) -> tuple:
        """締切前なら、申し込みを取り下げて、出走料を返す。"""
        with self._lock, self._conn:
            race = self.race_get(race_id)
            if race is None or race["status"] != "entry" or now >= race["entry_close"]:
                return False, fail("race_entry_closed")
            cur = self._conn.execute(
                "DELETE FROM horse_entries WHERE race_id=? AND horse_id=? AND owner_id=? AND lane<0",
                (race_id, horse_id, user_id),
            )
            if not cur.rowcount:
                return False, fail("race_not_entered")
            self._apply(user_id, horse_mod.ENTRY_FEE, "競馬:出走料の返金")
            return True, ""

    def race_close_entries(self, race_id: int, rng: Optional[random.Random] = None) -> Optional[dict]:
        """申し込みを締め切って、NPC で埋めた出走馬と、コースを決める（発売中にする）。{"runners", "track"} を返す。

        オッズは、シミュレーションに時間がかかるので、呼び出し側が計算して、race_set_odds で書く。
        """
        rng = rng or random.SystemRandom()
        with self._lock, self._conn:
            race = self.race_get(race_id)
            if race is None or race["status"] != "entry":
                return None
            registered = self.race_entries(race_id, registered_only=True)
            rows = []
            for e in registered:  # 申し込み済みの馬は、いまのステータスをコピーする（締切のあとの、にんじんは効かない）
                h = self.horse_get(e["horse_id"]) or e
                rows.append(
                    (e["horse_id"], e["owner_id"], h["name"], h["coat"], h["silks"])
                    + tuple(h[k] for k in horse_mod.STAT_KEYS)
                    + (h.get("races", 0), h.get("wins", 0), h.get("places", 0))  # 戦績のコピー（オッズに使う）
                )
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
                )
            rng.shuffle(rows)
            runners = []
            for lane, row in enumerate(rows, start=1):
                form = horse_mod.random_form(rng)
                self._conn.execute(
                    "INSERT INTO horse_entries (race_id, lane, horse_id, owner_id, name, coat, silks, speed, stamina, power, guts, "
                    "races, wins, places, form) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (race_id, lane) + row + (form,),
                )
                runners.append(
                    horse_mod.Runner(lane, row[2], row[5], row[6], row[7], row[8], form, row[9], row[10], row[11])
                )
            track = horse_mod.random_track(rng)
            self._conn.execute(
                "UPDATE horse_races SET status='betting', distance=?, going=? WHERE id=?",
                (track.distance, track.going, race_id),
            )
            return {"runners": runners, "track": track}

    def race_set_odds(self, race_id: int, odds) -> None:
        """オッズを書く。odds は、core/horse.py の make_all_odds の結果（{"win": {枠: オッズ}, "quinella": {キー: オッズ}, …}）。

        枠番の順の (単勝, 複勝) の並び（make_odds の結果）でもよい（組み合わせの馬券は、発売されない）。
        """
        if not isinstance(odds, dict):
            odds = {
                "win": {str(i): w for i, (w, _) in enumerate(odds, start=1)},
                "place": {str(i): p for i, (_, p) in enumerate(odds, start=1)},
            }
        with self._lock, self._conn:
            for lane in odds["win"]:
                self._conn.execute(
                    "UPDATE horse_entries SET odds_win=?, odds_place=? WHERE race_id=? AND lane=?",
                    (odds["win"][lane], odds["place"][lane], race_id, int(lane)),
                )
            self._conn.execute("DELETE FROM horse_odds WHERE race_id=?", (race_id,))
            for kind in horse_mod.COMBO_SIZE:
                self._conn.executemany(
                    "INSERT INTO horse_odds (race_id, kind, key, odds) VALUES (?, ?, ?, ?)",
                    [(race_id, kind, key, o) for key, o in odds.get(kind, {}).items()],
                )

    def race_odds(self, race_id: int, kind: str, lanes) -> Optional[float]:
        """その馬券のオッズ（なければ None）。lanes は、選んだ枠の並び（単勝・複勝は1つ）。"""
        with self._lock:
            if kind in ("win", "place"):
                r = self._conn.execute(
                    "SELECT odds_win, odds_place FROM horse_entries WHERE race_id=? AND lane=?", (race_id, lanes[0])
                ).fetchone()
                value = r and (r["odds_win"] if kind == "win" else r["odds_place"])
                return value if value else None
            r = self._conn.execute(
                "SELECT odds FROM horse_odds WHERE race_id=? AND kind=? AND key=?",
                (race_id, kind, horse_mod.combo_key(kind, lanes)),
            ).fetchone()
            return r["odds"] if r else None

    def race_bets(self, user_id: int, race_id: int) -> list:
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM horse_bets WHERE race_id=? AND user_id=? ORDER BY id", (race_id, user_id)
                )
            ]

    def race_bet(self, user_id: int, race_id: int, kind: str, lane, stake: int, now: float) -> tuple:
        """馬券を買う。賭けた時点のオッズで確定する。(成功か, 理由か馬券の辞書)。賭け金は、その場で引かれる。

        lane は、単勝・複勝なら枠番、組み合わせの馬券（馬連・ワイド・馬単・三連複）なら、選んだ枠番の並び
        （馬単は、1着・2着の順）。
        """
        if kind not in horse_mod.BET_KINDS:
            return False, fail("race_select_bet_kind")
        lanes = list(lane) if isinstance(lane, (list, tuple)) else [lane]
        need = horse_mod.COMBO_SIZE.get(kind, 1)
        if len(lanes) != need or len(set(lanes)) != need:
            return False, fail("race_select_distinct_horses", kind=kind, need=need)
        if not horse_mod.MIN_STAKE <= stake <= horse_mod.MAX_STAKE:
            return False, fail("race_stake_range", min_stake=horse_mod.MIN_STAKE, max_stake=horse_mod.MAX_STAKE)
        with self._lock, self._conn:
            race = self.race_get(race_id)
            if race is None or race["status"] != "betting":
                return False, fail("race_bet_unavailable")
            if now >= race["bet_close"]:
                return False, fail("race_bet_closed")
            names = []
            for x in lanes:
                entry = self._conn.execute(
                    "SELECT * FROM horse_entries WHERE race_id=? AND lane=?", (race_id, x)
                ).fetchone()
                if entry is None:
                    return False, fail("race_lane_not_running")
                names.append(entry["name"])
            odds = self.race_odds(race_id, kind, lanes)
            if odds is None:
                return False, fail("race_odds_pending")
            self._ensure(user_id)
            spent = self._conn.execute(
                "SELECT COALESCE(SUM(stake), 0) FROM horse_bets WHERE race_id=? AND user_id=?", (race_id, user_id)
            ).fetchone()[0]
            if spent + stake > horse_mod.MAX_RACE_STAKE:
                return False, fail("race_max_stake", max_race_stake=horse_mod.MAX_RACE_STAKE)
            if self._balance(user_id) < stake:
                return False, fail("insufficient_balance", balance=self._balance(user_id))
            label = "・".join(names)
            self._apply(user_id, -stake, f"競馬:馬券（{horse_mod.BET_KINDS[kind]}・{label}）")
            self._conn.execute(
                "UPDATE users SET total_wagered = total_wagered + ? WHERE guild_id=? AND user_id=?",
                (stake, self._g(), user_id),
            )
            picks = horse_mod.combo_key(kind, lanes) if kind in horse_mod.COMBO_SIZE else ""
            cur = self._conn.execute(
                "INSERT INTO horse_bets (guild_id, race_id, user_id, kind, lane, picks, stake, odds, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._g(), race_id, user_id, kind, lanes[0], picks, stake, odds, now),
            )
            return True, {
                "id": cur.lastrowid,
                "kind": kind,
                "lane": lanes[0],
                "lanes": lanes,
                "stake": stake,
                "odds": odds,
                "name": label,
            }

    def race_bet_many(self, user_id: int, race_id: int, kind: str, lanes: list, stake: int, now: float) -> tuple:
        """単勝・複勝を、何頭かまとめて買う（1頭ごとに stake ずつ、別々の馬券になる）。(成功か, 理由か馬券の辞書の並び)。

        先に、合計の賭け金（レースの上限・残高）を確かめてから買う。それでも途中で買えなくなったら、そこまでの馬券は残る。
        """
        if kind not in horse_mod.MULTI_KINDS:
            return False, fail("race_multi_only")
        lanes = list(dict.fromkeys(lanes))  # 同じ枠は1回だけ
        if not lanes:
            return False, fail("race_select_horses")
        if not horse_mod.MIN_STAKE <= stake <= horse_mod.MAX_STAKE:
            return False, fail("race_stake_range", min_stake=horse_mod.MIN_STAKE, max_stake=horse_mod.MAX_STAKE)
        with self._lock, self._conn:
            self._ensure(user_id)
            spent = self._conn.execute(
                "SELECT COALESCE(SUM(stake), 0) FROM horse_bets WHERE race_id=? AND user_id=?", (race_id, user_id)
            ).fetchone()[0]
            total = stake * len(lanes)
            if spent + total > horse_mod.MAX_RACE_STAKE:
                return False, fail(
                    "race_max_stake_many",
                    max_race_stake=horse_mod.MAX_RACE_STAKE,
                    total=total,
                    spent=spent,
                )
            if self._balance(user_id) < total:
                return False, fail("race_insufficient_balance_many", total=total, balance=self._balance(user_id))
            bought = []
            for lane in lanes:
                ok, result = self.race_bet(user_id, race_id, kind, lane, stake, now)
                if not ok:
                    return False, fail("race_partial_bought", inner=result, bought=len(bought)) if bought else result
                bought.append(result)
            return True, bought

    def race_run(self, race_id: int, rng: Optional[random.Random] = None, tz_hours: float = 9.0) -> Optional[dict]:
        """レースを走らせて、結果を決め、賞金と払い戻しを精算する。すでに終わっていれば None。

        結果は、ここで決まる（演出は、返した frames を見せるだけ）。(race, entries, order, frames, bets, prizes) を辞書で返す。
        """
        rng = rng or random.SystemRandom()
        with self._lock, self._conn:
            race = self.race_get(race_id)
            if race is None or race["status"] != "betting":
                return None
            self._conn.execute("UPDATE horse_races SET status='done' WHERE id=?", (race_id,))
            is_major = horse_mod.is_major_race(race, tz_hours)
            prizes_table = horse_mod.MAJOR_PRIZES if is_major else horse_mod.PRIZES
            entries = self.race_entries(race_id)
            styles = [horse_mod.style_of({k: e[k] for k in horse_mod.STAT_KEYS}) for e in entries]
            runners = [
                horse_mod.Runner(
                    e["lane"], e["name"], e["speed"], e["stamina"], e["power"], e["guts"], e["form"], style=s
                )
                for e, s in zip(entries, styles)
            ]
            track = horse_mod.Track(race["distance"], race["going"])
            sim = horse_mod.simulate(runners, track, rng, keep_frames=True)
            order = sim["order"]  # entries の添字の並び（着順）
            real_entries = sum(1 for e in entries if e["owner_id"])
            placed_total = sum(
                prizes_table[p - 1]
                for p, idx in enumerate(order, start=1)
                if p <= len(prizes_table) and entries[idx]["owner_id"]
            )
            topup = 0
            if is_major:
                shortfall = max(0, placed_total - real_entries * horse_mod.ENTRY_FEE)
                if shortfall:
                    state = self._bank_sync(time.time())
                    topup = min(shortfall, int(state["cash"]), horse_mod.MAJOR_HOUSE_TOPUP)
                    if topup:
                        self._bank_set(state["cash"] - topup, state["loaned"])
            scale = horse_mod.prize_pool_scale(real_entries, placed_total, topup)
            prizes, retired = {}, []
            for place, idx in enumerate(order, start=1):
                e = entries[idx]
                e["finish"] = place
                self._conn.execute("UPDATE horse_entries SET finish=? WHERE id=?", (place, e["id"]))
                prize = round((prizes_table[place - 1] if place <= len(prizes_table) else 0) * scale)
                if e["owner_id"]:
                    horse = self.horse_get(e["horse_id"])
                    if prize:
                        prize = round(
                            prize * (1 + self._relic_buffs(e["owner_id"]).get("prize", 0.0))
                        )  # 発掘品（竜の涙）の効果
                        rate = teiai.HORSE_PRIZE_SKIM_RATE
                        if horse is not None and horse["rented_day"]:
                            rate += teiai.HORSE_RENTAL_EXTRA_SKIM_RATE  # レンタル馬は、持ち馬よりさらにピンハネされる
                        skim = round(prize * rate)
                        net = prize - skim
                        self._apply(e["owner_id"], net, f"競馬賞金:{e['name']}（{place}着）", income=net)
                        prizes[e["owner_id"]] = prizes.get(e["owner_id"], 0) + net
                        prize = net  # 戦績・earnings には、手取り（ピンハネ後）を残す
                    self._conn.execute(
                        "UPDATE horses SET races=races+1, wins=wins+?, places=places+?, earnings=earnings+? WHERE id=?",
                        (1 if place == 1 else 0, 1 if place <= 3 else 0, prize, e["horse_id"]),
                    )
                    horse = self.horse_get(e["horse_id"])
                    if horse is not None and horse["races"] >= horse_mod.MAX_RACES:  # 定年：自動で名誉引退
                        value = horse_mod.retire_value(horse["races"], horse["wins"], horse["places"])
                        self._ensure(e["owner_id"])
                        if value:
                            self._apply(
                                e["owner_id"],
                                value,
                                f"競馬:定年引退{'金' if value > 0 else '費用'}（{horse['name']}）",
                                income=max(0, value),
                            )
                        self._conn.execute("UPDATE horses SET owner_id=-2 WHERE id=?", (horse["id"],))
                        retired.append({**horse, "value": value})
            order_lanes = [entries[i]["lane"] for i in order]
            top_lane, top3 = order_lanes[0], set(order_lanes[:3])
            bets, lost = [], 0
            for b in self._conn.execute(
                "SELECT * FROM horse_bets WHERE race_id=? AND settled=0 ORDER BY id", (race_id,)
            ).fetchall():
                b = dict(b)
                if b["kind"] == "win":
                    hit = b["lane"] == top_lane
                elif b["kind"] == "place":
                    hit = b["lane"] in top3
                else:
                    sep = ">" if b["kind"] == "exacta" else "-"
                    hit = horse_mod.combo_hit(b["kind"], [int(x) for x in b["picks"].split(sep)], order_lanes)
                pay = horse_mod.payout(b["stake"], b["odds"]) if hit else 0
                if pay:
                    self._apply(b["user_id"], pay, "競馬:払い戻し", income=max(0, pay - b["stake"]))
                else:
                    lost += b["stake"]
                self._conn.execute("UPDATE horse_bets SET payout=?, settled=1 WHERE id=?", (pay, b["id"]))
                self._conn.execute(
                    "UPDATE users SET games_played = games_played + 1, total_won = total_won + ?, "
                    "biggest_win = MAX(biggest_win, ?) WHERE guild_id=? AND user_id=?",
                    (pay, pay - b["stake"], self._g(), b["user_id"]),
                )
                b["payout"] = pay
                bets.append(b)
            self._bank_income(
                int(lost * teiai.LOSS_SHARE), 1.0, needs_depositors=True
            )  # 負けた額の一部が、預金者の利回り
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
                "styles": dict(zip((e["lane"] for e in entries), styles)),
            }
