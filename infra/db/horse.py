"""競馬（Economy に混ぜ込む部品）。馬・レース・出走・馬券・精算。数字とシミュレーションは core/horse.py。

流れ: 定時レースを用意（entry）→ 申し込みの締切で、出走馬を決めて NPC で埋め、オッズを付ける（betting）→ 馬券の締切（発走の2分前）
→ 発走で、結果を先に決めて精算（done）。演出（アニメーション）は、そのあとに見せるだけ。表は core/schema.py。
"""

import datetime
import random
import time
from collections import Counter
from typing import Optional

from domain import horse as horse_mod
from domain import shop as shop_mod
from domain import teiai
from domain.shop import CARROT, GOLD_CARROT, HORSE_VOUCHER
from domain.types import PurchaseResult
from infra.db.base import EcoBase


class HorseMixin(EcoBase):
    # ---------------------------------------------------------------- #
    # 馬                                                                #
    # ---------------------------------------------------------------- #
    def _consume(self, user_id: int, item_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE inventory SET qty = qty - 1 WHERE guild_id=? AND user_id=? AND item_id=? AND qty>0",
            (self._g(), user_id, item_id),
        )
        return bool(cur.rowcount)

    def horse_create(self, user_id: int, name: str, coat: str, silks: str) -> tuple:
        """購入券を1枚使って、競走馬を迎える。(成功か, 理由か馬の番号)。ステータスは、全部 50 から始まる。

        いまはプレイヤー向けの入口ではない（`/競馬 厩舎` は horse_buy_rental に一本化）。テスト・管理用に残す。
        """
        ok, name = horse_mod.check_name(name)
        if not ok:
            return False, name
        if coat not in horse_mod.COATS or silks not in horse_mod.SILKS:
            return False, "毛色と勝負服の色を、選び直してください。"
        with self._lock, self._conn:
            self._ensure(user_id)
            owned = self._horse_count(user_id)  # 出品中の馬も数える
            if owned >= horse_mod.MAX_HORSES:
                return False, f"持てる馬は {horse_mod.MAX_HORSES} 頭までです。"
            if not self._consume(user_id, HORSE_VOUCHER):
                return False, "「競走馬（購入券）」を持っていません。ショップで買えます。"
            s = horse_mod.DEFAULT_STATS
            cur = self._conn.execute(
                "INSERT INTO horses (guild_id, owner_id, name, coat, silks, speed, stamina, power, guts, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._g(), user_id, name, coat, silks, s["speed"], s["stamina"], s["power"], s["guts"], time.time()),
            )
            return True, cur.lastrowid

    @staticmethod
    def _today(now: float, tz_hours: float) -> str:
        tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
        return datetime.datetime.fromtimestamp(now, tz).date().isoformat()

    def rental_ensure(self, user_id: int, now: float, tz_hours: float, rng=None) -> dict:
        """今日のレンタル馬（帝愛から、その日だけ借りる馬）。無ければランダムなステータスで1頭作る。

        古い日のレンタル（買わなかった分）は消す（出走予定で未精算のものは残す）。誰でも、1日1頭まで。
        """
        rng = rng or random.SystemRandom()
        day = self._today(now, tz_hours)
        with self._lock, self._conn:
            self._ensure(user_id)
            cur = self._conn.execute(
                "SELECT * FROM horses WHERE guild_id=? AND owner_id=? AND rented_day=?", (self._g(), user_id, day)
            ).fetchone()
            if cur:
                return dict(cur)
            self._conn.execute(
                "DELETE FROM horses WHERE guild_id=? AND owner_id=? AND rented_day IS NOT NULL AND rented_day!=? "
                "AND id NOT IN (SELECT e.horse_id FROM horse_entries e JOIN horse_races r ON r.id=e.race_id "
                "WHERE r.status IN ('entry','betting'))",
                (self._g(), user_id, day),
            )
            s = horse_mod.random_stats(rng, horse_mod.RENTAL_STAT_TOTAL)  # 買った馬より、合計を10低くする
            name = rng.choice(horse_mod.NPC_NAMES)
            coat, silks = rng.choice(horse_mod.COATS), rng.choice(horse_mod.SILKS)
            ins = self._conn.execute(
                "INSERT INTO horses (guild_id, owner_id, name, coat, silks, speed, stamina, power, guts, created, rented_day) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self._g(), user_id, name, coat, silks, s["speed"], s["stamina"], s["power"], s["guts"], now, day),
            )
            return dict(self._conn.execute("SELECT * FROM horses WHERE id=?", (ins.lastrowid,)).fetchone())

    def horse_buy_rental(self, user_id: int, now: float, tz_hours: float) -> tuple:
        """いまレンタルしている馬を、購入券1枚で自分の馬にする（ステータス・名前・戦績はそのまま）。馬を増やす唯一の方法。"""
        day = self._today(now, tz_hours)
        with self._lock, self._conn:
            self._ensure(user_id)
            row = self._conn.execute(
                "SELECT * FROM horses WHERE guild_id=? AND owner_id=? AND rented_day=?", (self._g(), user_id, day)
            ).fetchone()
            if row is None:
                return False, "いまレンタルしている馬がいません。"
            owned = self._horse_count(user_id)
            if owned >= horse_mod.MAX_HORSES:
                return False, f"持てる馬は {horse_mod.MAX_HORSES} 頭までです。"
            if not self._consume(user_id, HORSE_VOUCHER):
                return False, "「競走馬（購入券）」を持っていません。ショップで買えます。"
            self._conn.execute("UPDATE horses SET rented_day=NULL WHERE id=?", (row["id"],))
            return True, row["id"]

    def horse_list(self, user_id: int) -> list:
        with self._lock:
            return [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM horses WHERE guild_id=? AND owner_id=? ORDER BY id", (self._g(), user_id)
                )
            ]

    def horse_get(self, horse_id: int) -> Optional[dict]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM horses WHERE id=? AND guild_id=?", (horse_id, self._g())).fetchone()
            return dict(r) if r else None

    def horse_carrot(self, user_id: int, horse_id: int, src: str, dst: str) -> tuple:
        """にんじん1本で、ステータスを CARROT_POINTS 点、src から dst へ動かす。(成功か, 理由か新しいステータス)。"""
        with self._lock, self._conn:
            h = self.horse_get(horse_id)
            if h is None or h["owner_id"] != user_id:
                return False, "自分の馬を選んでください。"
            if h["rented_day"] is not None:
                return False, "レンタル馬は、ステータスを調整できません（買うと調整できます）。"
            ok, result = horse_mod.move_points({k: h[k] for k in horse_mod.STAT_KEYS}, src, dst)
            if not ok:
                return False, result
            if not self._consume(user_id, CARROT):
                return False, "「にんじん」を持っていません。ショップで買えます。"
            self._conn.execute(
                "UPDATE horses SET speed=?, stamina=?, power=?, guts=? WHERE id=?",
                (result["speed"], result["stamina"], result["power"], result["guts"], horse_id),
            )
            return True, result

    def horse_retire(self, user_id: int, horse_id: int) -> tuple:
        """馬を引退させる。戦績に応じて、引退金をもらう（戦績が悪いと、引退費用を払う）。(成功か, 理由かもらった額（払ったらマイナス）)。

        出走予定（申し込み中・発売中）の馬は、引退できない。引退した馬は、持ち馬から消える（owner_id=-2）。
        """
        with self._lock, self._conn:
            h = self.horse_get(horse_id)
            if h is None or h["owner_id"] != user_id:
                return False, "自分の馬を選んでください。"
            if h["rented_day"] is not None:
                return False, "レンタル馬は引退できません（買わなければ、日が替わると自然に消えます）。"
            busy = self._conn.execute(
                "SELECT 1 FROM horse_entries e JOIN horse_races r ON r.id=e.race_id "
                "WHERE e.horse_id=? AND r.status IN ('entry','betting')",
                (horse_id,),
            ).fetchone()
            if busy:
                return False, "出走予定のレースが終わるまで、引退できません。"
            value = horse_mod.retire_value(h["races"], h["wins"], h["places"])
            self._ensure(user_id)
            if value < 0 and self._balance(user_id) < -value:
                return False, f"引退費用 {-value:,}ペリカ が足りません（残高 {self._balance(user_id):,}ペリカ）。"
            if value:
                self._apply(
                    user_id,
                    value,
                    f"競馬:引退{'金' if value > 0 else '費用'}（{h['name']}）",
                    income=max(0, value),
                )
            self._conn.execute("UPDATE horses SET owner_id=-2 WHERE id=?", (horse_id,))
            return True, value

    def horse_gold_carrot(self, user_id: int, horse_id: int, first: str, second: str) -> tuple:
        """金にんじん1本で、first と second に5点ずつ足す（ステータスの合計が10点増える。1頭に GOLD_CARROT_MAX 本まで）。"""
        with self._lock, self._conn:
            h = self.horse_get(horse_id)
            if h is None or h["owner_id"] != user_id:
                return False, "自分の馬を選んでください。"
            if h["rented_day"] is not None:
                return False, "レンタル馬は、ステータスを調整できません（買うと調整できます）。"
            if h["bonus"] >= horse_mod.GOLD_CARROT_MAX * 2 * horse_mod.GOLD_CARROT_POINTS:
                return False, f"この馬には、金にんじんをもう使えません（1頭に {horse_mod.GOLD_CARROT_MAX} 本まで）。"
            ok, result = horse_mod.add_points({k: h[k] for k in horse_mod.STAT_KEYS}, first, second)
            if not ok:
                return False, result
            if not self._consume(user_id, GOLD_CARROT):
                return False, "「金にんじん」を持っていません。ショップで買えます。"
            self._conn.execute(
                "UPDATE horses SET speed=?, stamina=?, power=?, guts=?, bonus=bonus+? WHERE id=?",
                (
                    result["speed"],
                    result["stamina"],
                    result["power"],
                    result["guts"],
                    2 * horse_mod.GOLD_CARROT_POINTS,
                    horse_id,
                ),
            )
            return True, result

    # ---------------------------------------------------------------- #
    # レースの用意                                                      #
    # ---------------------------------------------------------------- #
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

    @staticmethod
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

    def race_ensure_scheduled(self, now: float, tz_hours: float, channel_id: int = 0) -> Optional[int]:
        """次の定時レースが、申し込みを始める時刻になっていて、まだ無ければ作る（申し込み受付を始める）。作ったレースの番号を返す。"""
        with self._lock, self._conn:
            start = self.race_slots(now, tz_hours)[0]
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

    # ---------------------------------------------------------------- #
    # 出走の申し込み                                                    #
    # ---------------------------------------------------------------- #
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
                return False, "このレースの申し込みは、もう締め切りました。"
            h = self.horse_get(horse_id)
            if h is None or h["owner_id"] != user_id:
                return False, "自分の馬を選んでください。"
            if h["races"] >= horse_mod.MAX_RACES:
                return (
                    False,
                    f"この馬は定年（{horse_mod.MAX_RACES}戦）に達したので、出走できません（引退済みのはずです）。",
                )
            rows = self.race_entries(race_id, registered_only=True)
            if any(e["owner_id"] == user_id for e in rows):
                return False, "1人が1つのレースに出せる馬は、1頭までです。"
            if len(rows) >= horse_mod.FIELD_SIZE:
                return False, "出走枠が埋まりました。"
            if any(e["name"] == h["name"] for e in rows):
                return False, "同じ名前の馬がすでに出走します。"
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
                return False, "この馬は、ほかのレースに出走予定です。"
            fee = horse_mod.ENTRY_FEE
            self._ensure(user_id)
            if self._balance(user_id) < fee:
                return False, f"出走料 {fee:,}ペリカ が足りません（残高 {self._balance(user_id):,}ペリカ）。"
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
                return False, "このレースの申し込みは、もう締め切りました。"
            cur = self._conn.execute(
                "DELETE FROM horse_entries WHERE race_id=? AND horse_id=? AND owner_id=? AND lane<0",
                (race_id, horse_id, user_id),
            )
            if not cur.rowcount:
                return False, "この馬は、申し込んでいません。"
            self._apply(user_id, horse_mod.ENTRY_FEE, "競馬:出走料の返金")
            return True, ""

    # ---------------------------------------------------------------- #
    # 申し込みの締切 → 出走馬・コース・オッズ                          #
    # ---------------------------------------------------------------- #
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
            # 帝愛の馬の勝負服は、ほかの馬と被らない色にする（プレイヤーの馬どうしが被るのは、そのまま）。
            # 色が足りないときだけ、いちばん使われていない色を重ねる
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

    # ---------------------------------------------------------------- #
    # 馬券                                                              #
    # ---------------------------------------------------------------- #
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
            return False, "馬券の種類を選んでください。"
        lanes = list(lane) if isinstance(lane, (list, tuple)) else [lane]
        need = horse_mod.COMBO_SIZE.get(kind, 1)
        if len(lanes) != need or len(set(lanes)) != need:
            return False, f"{horse_mod.BET_KINDS[kind]}は、違う馬を {need} 頭選んでください。"
        if not horse_mod.MIN_STAKE <= stake <= horse_mod.MAX_STAKE:
            return False, f"賭け金は {horse_mod.MIN_STAKE:,}〜{horse_mod.MAX_STAKE:,}ペリカ にしてください。"
        with self._lock, self._conn:
            race = self.race_get(race_id)
            if race is None or race["status"] != "betting":
                return False, "このレースの馬券は、まだ（またはもう）買えません。"
            if now >= race["bet_close"]:
                return False, "馬券の発売は、締め切りました。"
            names = []
            for x in lanes:
                entry = self._conn.execute(
                    "SELECT * FROM horse_entries WHERE race_id=? AND lane=?", (race_id, x)
                ).fetchone()
                if entry is None:
                    return False, "その馬は出走しません。"
                names.append(entry["name"])
            odds = self.race_odds(race_id, kind, lanes)
            if odds is None:
                return False, "オッズを計算中です。少し待ってから、もう一度買ってください。"
            self._ensure(user_id)
            spent = self._conn.execute(
                "SELECT COALESCE(SUM(stake), 0) FROM horse_bets WHERE race_id=? AND user_id=?", (race_id, user_id)
            ).fetchone()[0]
            if spent + stake > horse_mod.MAX_RACE_STAKE:
                return False, f"1つのレースに賭けられるのは、合計 {horse_mod.MAX_RACE_STAKE:,}ペリカ までです。"
            if self._balance(user_id) < stake:
                return False, f"残高が足りません（いまの残高: {self._balance(user_id):,}ペリカ）。"
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
            return False, "まとめて買えるのは、単勝と複勝だけです。"
        lanes = list(dict.fromkeys(lanes))  # 同じ枠は1回だけ
        if not lanes:
            return False, "馬を選んでください。"
        if not horse_mod.MIN_STAKE <= stake <= horse_mod.MAX_STAKE:
            return False, f"賭け金は {horse_mod.MIN_STAKE:,}〜{horse_mod.MAX_STAKE:,}ペリカ にしてください。"
        with self._lock, self._conn:
            self._ensure(user_id)
            spent = self._conn.execute(
                "SELECT COALESCE(SUM(stake), 0) FROM horse_bets WHERE race_id=? AND user_id=?", (race_id, user_id)
            ).fetchone()[0]
            total = stake * len(lanes)
            if spent + total > horse_mod.MAX_RACE_STAKE:
                return (
                    False,
                    f"1つのレースに賭けられるのは、合計 {horse_mod.MAX_RACE_STAKE:,}ペリカ までです"
                    f"（今回は {total:,}、すでに {spent:,}）。",
                )
            if self._balance(user_id) < total:
                return (
                    False,
                    f"残高が足りません（合計 {total:,}ペリカ 必要。いまの残高: {self._balance(user_id):,}ペリカ）。",
                )
            bought = []
            for lane in lanes:
                ok, result = self.race_bet(user_id, race_id, kind, lane, stake, now)
                if not ok:
                    return False, f"{result}（先の {len(bought)} 頭ぶんは買えています）" if bought else result
                bought.append(result)
            return True, bought

    # ---------------------------------------------------------------- #
    # 発走・精算                                                        #
    # ---------------------------------------------------------------- #
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
            # 原資（人間の馬が払った出走料の合計）を超えて、賞金で新しいペリカが生まれないよう、足りなければ3着まで同じ割合で削る
            real_entries = sum(1 for e in entries if e["owner_id"])
            placed_total = sum(
                prizes_table[p - 1]
                for p, idx in enumerate(order, start=1)
                if p <= len(prizes_table) and entries[idx]["owner_id"]
            )
            topup = 0
            if (
                is_major
            ):  # 重賞は、出走料だけで足りなければ、帝愛の手元現金から補う（新しくは作らない。原資が無ければ補わない）
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

    # ---------------------------------------------------------------- #
    # 破産の差し押さえと、帝愛の競売                                    #
    # ---------------------------------------------------------------- #
    HORSE_PRICE = shop_mod.BY_ID[HORSE_VOUCHER].price  # 馬の価値（差し押さえの換金額と、競売の定価の元）

    def _seize_horses(self, user_id: int, now: float) -> int:
        """破産で、持ち馬をすべて差し押さえて、帝愛の競売に出す。換金の元になる額（馬1頭 = 購入券の値段）を返す。

        出走の申し込み中の馬は取り下げ（出走料は戻らない）。すでに出走馬に決まっている馬は、帝愛の馬（NPC）に替わる。
        """
        rows = self._conn.execute(
            "SELECT id FROM horses WHERE guild_id=? AND owner_id=? AND rented_day IS NULL", (self._g(), user_id)
        ).fetchall()
        for r in rows:
            self._conn.execute("UPDATE horses SET owner_id=-1, listed_at=? WHERE id=?", (now, r["id"]))
            self._conn.execute("DELETE FROM horse_entries WHERE horse_id=? AND lane<0", (r["id"],))
            self._conn.execute(
                "UPDATE horse_entries SET owner_id=0, horse_id=0 WHERE horse_id=? AND race_id IN "
                "(SELECT id FROM horse_races WHERE status IN ('entry','betting'))",
                (r["id"],),
            )
        return len(rows) * self.HORSE_PRICE

    def horse_auction_price(self, listed_at: float, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        return max(1, round(self.HORSE_PRICE * teiai.auction_rate(listed_at, now)))

    def auction_horses(self, now: Optional[float] = None) -> list:
        """帝愛の競売に出ている馬。[{馬の行, price, rate}]（出品の早い順）。"""
        now = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM horses WHERE guild_id=? AND owner_id=-1 ORDER BY listed_at, id", (self._g(),)
            ).fetchall()
        return [
            {
                **dict(r),
                "price": self.horse_auction_price(r["listed_at"], now),
                "rate": teiai.auction_rate(r["listed_at"], now),
            }
            for r in rows
        ]

    def auction_buy_horse(self, user_id: int, horse_id: int, now: Optional[float] = None) -> PurchaseResult:
        """競売の馬を買う。カタギだけ・持てるのは MAX_HORSES 頭まで。名前とステータスは、そのまま引き継ぐ。"""
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._ensure(user_id)
            if self.status(user_id, now) != teiai.KATAGI:
                return PurchaseResult(
                    False, "競売で買えるのは、借金も延滞もないカタギだけです。", self._balance(user_id)
                )
            row = self._conn.execute(
                "SELECT * FROM horses WHERE id=? AND guild_id=? AND owner_id=-1", (horse_id, self._g())
            ).fetchone()
            if row is None:
                return PurchaseResult(False, "その馬は、もう売れてしまいました。", self._balance(user_id))
            owned = self._horse_count(user_id)  # 出品中の馬も数える
            if owned >= horse_mod.MAX_HORSES:
                return PurchaseResult(False, f"持てる馬は {horse_mod.MAX_HORSES} 頭までです。", self._balance(user_id))
            price = self.horse_auction_price(row["listed_at"], now)
            if self._balance(user_id) < price:
                return PurchaseResult(
                    False,
                    f"ペリカが足りません（{price:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。",
                    self._balance(user_id),
                )
            self._apply(user_id, -price, f"競売:競走馬（{row['name']}）")
            self._conn.execute("UPDATE horses SET owner_id=?, listed_at=NULL WHERE id=?", (user_id, horse_id))
            self._bank_income(price, teiai.AUCTION_YIELD_SHARE, now)
            return PurchaseResult(True, balance=self._balance(user_id))
