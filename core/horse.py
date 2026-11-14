"""競馬の数字と、レースのシミュレーション。Discord と DB に依存しない純粋なロジック。

- 馬のステータスは4つ（スピード・スタミナ・パワー・根性）で、**合計は固定**（STAT_TOTAL）。強さは買えず、配分（作り）だけを選ぶ。
  にんじん1本で、CARROT_POINTS 点を、あるステータスから別のステータスへ動かせる。
- コースは、距離（短・中・長）と馬場（良・稍重・重）。短距離はスピード、長距離はスタミナ、重い馬場はパワーが効く。
- レースは、1ティックごとに全馬が進み、毎ティックの運（ノイズ）と、レース全体の運がある。結果は、シード（乱数）で決まる。
- オッズは、出走馬の**戦績**（出走数・勝ち数・3着以内の数）で決める。出走が少ない馬は平均に寄せ、初出走は高めにし、調子（◎○△）で動かす。
  ステータスはオッズに入らない（実際の強さと戦績のずれが、狙い目になる）。控除（TAKEOUT）を引く。
  レースの結果は、ステータスのシミュレーションで決まる（estimate は、その本当の確率を調べる検証用）。
"""

import datetime
import itertools
import math
import random
from dataclasses import dataclass
from typing import Any, Optional

from core import catalogs

_CFG = catalogs.horse()

STAT_KEYS = tuple(s.key for s in _CFG.stats)
STAT_NAMES = {s.key: s.name for s in _CFG.stats}
STAT_TOTAL = _CFG.stat_total
RENTAL_STAT_TOTAL = STAT_TOTAL - _CFG.rental_stat_penalty
STAT_MIN, STAT_MAX = _CFG.stat_min, _CFG.stat_max
DEFAULT_STATS = {s.key: s.default for s in _CFG.stats}
CARROT_POINTS = _CFG.carrot_points

GOLD_CARROT_POINTS = _CFG.gold_carrot_points
GOLD_CARROT_MAX = _CFG.gold_carrot_max

FIELD_SIZE = _CFG.field_size
NAME_MAX = _CFG.name_max
MAX_HORSES = _CFG.max_horses

COATS = list(_CFG.coats)
SILKS = list(_CFG.silks)

DISTANCES = _CFG.distances
GOINGS = tuple(g.name for g in _CFG.goings)
GOING_PENALTY = {g.name: g.penalty for g in _CFG.goings}
GOING_WEIGHT = tuple(g.weight for g in _CFG.goings)
FORM_MARK = {f.value: f.mark for f in _CFG.forms}
FORM_MULT = {f.value: f.mult for f in _CFG.forms}

TAKEOUT = _CFG.takeout
ODDS_SIMS = _CFG.odds_sims
MAX_ODDS = _CFG.max_odds
MIN_STAKE, MAX_STAKE = _CFG.min_stake, _CFG.max_stake
MAX_RACE_STAKE = _CFG.max_race_stake

ENTRY_FEE = _CFG.entry_fee
PRIZES = _CFG.prizes

MAJOR_HOUR = _CFG.major_hour
MAJOR_PRIZES = _CFG.major_prizes
MAJOR_HOUSE_TOPUP = _CFG.major_house_topup


def is_major_race(race: dict, tz_hours: float = 9.0) -> bool:
    """定時レースで、発走が MAJOR_HOUR（21時）なら重賞。"""
    tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
    return race["kind"] == "regular" and datetime.datetime.fromtimestamp(race["start_at"], tz).hour == MAJOR_HOUR


def prize_pool_scale(real_entries: int, placed_prizes: int, topup: int = 0) -> float:
    """そのレースで、人間の馬が払った出走料の合計（＋topup）が、賞金（placed_prizes＝人間の馬が獲る分の合計）に足りるか。

    足りれば1.0（そのまま払う）。足りなければ、その比率ぶん、3着まで同じ割合で削る。
    topup は、重賞などで帝愛の手元現金から補った分。出走料だけでは、新しいペリカはレースごとに生まれない。
    """
    if placed_prizes <= 0:
        return 1.0
    return min(1.0, (real_entries * ENTRY_FEE + max(0, topup)) / placed_prizes)


TICKS = _CFG.ticks
MAX_TICKS = _CFG.max_ticks


@dataclass(frozen=True)
class Runner:
    """出走する1頭（レースの時点の、ステータスのコピー）。"""

    lane: int
    name: str
    speed: int
    stamina: int
    power: int
    guts: int
    form: int = 0  # 1 ◎ / 0 ○ / -1 △
    races: int = 0  # 戦績（オッズに使う。レースの結果には効かない）
    wins: int = 0
    places: int = 0  # 3着以内（1着を含む）
    style: Optional[str] = None  # 脚質（未指定なら、ステータスから自然に決める。STYLE_NAMES のキー）

    def stat(self, key: str) -> int:
        return getattr(self, key)

    @property
    def effective_style(self) -> str:
        return self.style or style_of({k: getattr(self, k) for k in STAT_KEYS})


@dataclass(frozen=True)
class Track:
    distance: int
    going: str

    @property
    def label(self) -> str:
        return f"{self.distance:,}m・{self.going}"


# ---- 脚質（逃げ・先行・差し・追込） ---- #
STYLE_NAMES = {s.key: s.name for s in _CFG.styles}
STYLE_MARK = {s.key: s.mark for s in _CFG.styles}
STYLE_BY_STAT = {s.key: s.style for s in _CFG.stats}


def style_of(stats: dict) -> str:
    """ステータスの配分から、自然な脚質を決める（いちばん高いステータスで決まる。並んだら speed→power→stamina→guts の順を優先）。"""
    best = max(STAT_KEYS, key=lambda k: (stats[k], -STAT_KEYS.index(k)))
    return STYLE_BY_STAT[best]


def valid_stats(stats: dict) -> bool:
    """合計が STAT_TOTAL ちょうどで、すべてが範囲内か。"""
    return (
        set(stats) == set(STAT_KEYS)
        and sum(stats.values()) == STAT_TOTAL
        and all(STAT_MIN <= v <= STAT_MAX for v in stats.values())
    )


MAX_RACES = _CFG.max_races
RETIRE_WIN = _CFG.retire_win
RETIRE_PLACE = _CFG.retire_place
RETIRE_LOSS = _CFG.retire_loss
RETIRE_MAX, RETIRE_FEE_MAX = _CFG.retire_max, _CFG.retire_fee_max


def retire_value(races: int, wins: int, places: int) -> int:
    """引退のとき、もらう額（マイナスなら払う額）。戦績が良いほど増え、悪いと引退費用になる。"""
    value = wins * RETIRE_WIN + max(0, places - wins) * RETIRE_PLACE - max(0, races - places) * RETIRE_LOSS
    return max(-RETIRE_FEE_MAX, min(RETIRE_MAX, value))


def add_points(stats: dict, first: str, second: str) -> tuple:
    """金にんじん1本ぶん、first と second に GOLD_CARROT_POINTS 点ずつ足す（同じステータスでもよい）。(成功か, 理由か新しいステータス)。"""
    if first not in STAT_KEYS or second not in STAT_KEYS:
        return False, "増やすステータスを選んでください。"
    new = dict(stats)
    for key in (first, second):
        new[key] += GOLD_CARROT_POINTS
    for key in (first, second):
        if new[key] > STAT_MAX:
            return False, f"{STAT_NAMES[key]} は、これ以上は増やせません（最高 {STAT_MAX}）。"
    return True, new


def move_points(stats: dict, src: str, dst: str, carrots: int = 1) -> tuple:
    """にんじん carrots 本ぶん、src から dst へポイントを動かす。(成功か, 理由か新しいステータス)。"""
    if src == dst or src not in STAT_KEYS or dst not in STAT_KEYS:
        return False, "動かす元と先は、別のステータスを選んでください。"
    pts = CARROT_POINTS * carrots
    if stats[src] - pts < STAT_MIN:
        return False, f"{STAT_NAMES[src]} は、これ以上は減らせません（最低 {STAT_MIN}）。"
    if stats[dst] + pts > STAT_MAX:
        return False, f"{STAT_NAMES[dst]} は、これ以上は増やせません（最高 {STAT_MAX}）。"
    new = dict(stats)
    new[src] -= pts
    new[dst] += pts
    return True, new


def random_stats(rng: random.Random, total: int = STAT_TOTAL) -> dict:
    """範囲内で、合計が total になる、ランダムな配分（NPC・レンタル馬用）。"""
    while True:
        raw = [rng.random() ** 0.8 + 0.25 for _ in STAT_KEYS]
        scale = (total - STAT_MIN * len(STAT_KEYS)) / sum(raw)
        vals = [STAT_MIN + int(r * scale) for r in raw]
        vals[0] += total - sum(vals)  # 端数は先頭に寄せる
        stats = dict(zip(STAT_KEYS, vals))
        if (
            set(stats) == set(STAT_KEYS)
            and sum(stats.values()) == total
            and all(STAT_MIN <= v <= STAT_MAX for v in stats.values())
        ):
            return stats


def random_track(rng: random.Random) -> Track:
    return Track(rng.choice(DISTANCES), rng.choices(GOINGS, weights=GOING_WEIGHT, k=1)[0])


def random_form(rng: random.Random) -> int:
    return rng.choices([1, 0, -1], weights=[2, 5, 2], k=1)[0]


# ---- シミュレーション ---- #
def speed_coef(distance: int) -> float:
    """距離ごとの、スピードの効き（短いほど効く）。1800m で 0.05。"""
    return min(0.09, 0.05 * 1800 / distance)


FATIGUE_MAX = _CFG.fatigue_max
NOISE = _CFG.noise
LUCK = _CFG.luck

# ---- 脚質による展開（逃げ・先行が多いほどハイペースになり、終盤は差し・追込が伸びる） ---- #
PACE_FACTOR = _CFG.pace_factor
STYLE_EARLY = {s.key: s.early for s in _CFG.styles}
STYLE_LATE = {s.key: s.late for s in _CFG.styles}


def style_curve(style: str, p: float, pace_shift: float) -> float:
    """脚質による、いまの位置（p）での加減速（率）。ハイペースだと、前寄りの脚質は早めに苦しくなり、後ろの脚質が伸びる。"""
    if p < 0.35:
        v = STYLE_EARLY[style]
        if style in ("nige", "senko"):
            v -= pace_shift
        return v
    if p >= 0.70:
        v = STYLE_LATE[style]
        if style in ("sashi", "oikomi"):
            v += pace_shift
        return v
    t = (p - 0.35) / 0.35  # 中間は、序盤と終盤を線形に補間
    return STYLE_EARLY[style] * (1 - t) + STYLE_LATE[style] * t


def _mult(r: Runner, p: float, track: Track, luck: float, spurt: float, style: str, pace_shift: float) -> float:
    dfac = track.distance / 1800
    m = 1 + (r.speed - 50) / 50 * speed_coef(track.distance)
    m *= 1 + (r.power - 50) / 50 * 0.05 * max(0.0, 1 - p / 0.2)  # 出足（パワー）
    reach = min(1.0, max(0.25, 0.8 * (r.stamina / 50) / dfac))  # ここまでは、バテずに走れる
    if p > reach and reach < 1.0:
        m *= 1 - FATIGUE_MAX * (p - reach) / (1 - reach)
    if p >= 0.75:
        m *= 1 + spurt  # ラストスパート（根性）
    m *= 1 + style_curve(style, p, pace_shift)  # 脚質・展開
    m *= 1 - GOING_PENALTY[track.going] * (1 - (r.power - 20) / 60 * 0.7)  # 馬場
    return m * FORM_MULT[r.form] * (1 + luck)


def simulate(runners: list, track: Track, rng: random.Random, keep_frames: bool = False) -> dict[str, Any]:
    """1レース走らせる。{"order": 着順（lane の並び）, "times": 着順ごとのゴール時刻, "frames": ティックごとの進み（keep_frames のとき）}。"""
    n = len(runners)
    pos = [0.0] * n
    finish: list[Optional[float]] = [None] * n
    luck = [rng.gauss(0, LUCK) for _ in runners]
    spurt = [max(0.0, (r.guts - 20) / 60 * 0.08 * rng.uniform(0.5, 1.5)) for r in runners]  # ラストスパート（根性）
    styles = [r.effective_style for r in runners]
    pace = sum(1 for s in styles if s in ("nige", "senko")) / n if n else 0.5
    pace_shift = (pace - 0.5) * PACE_FACTOR
    frames: list[list[float]] = [[0.0] * n] if keep_frames else []
    for t in range(1, MAX_TICKS + 1):
        for i, r in enumerate(runners):
            if finish[i] is not None:
                continue
            step = _mult(r, pos[i], track, luck[i], spurt[i], styles[i], pace_shift) * (1 + rng.gauss(0, NOISE)) / TICKS
            new = pos[i] + max(0.0, step)
            if new >= 1.0:
                finish[i] = t - 1 + (1.0 - pos[i]) / max(1e-9, new - pos[i])  # ゴールを越えた時刻（ティック内で補間）
                new = 1.0
            pos[i] = new
        if keep_frames:
            frames.append(list(pos))
        if all(f is not None for f in finish):
            break
    final_times = [MAX_TICKS + (1 - pos[i]) if finish[i] is None else finish[i] for i in range(n)]
    order = sorted(range(n), key=lambda i: (final_times[i], rng.random()))
    out: dict[str, Any] = {"order": order, "times": [final_times[i] for i in order], "finish": final_times}
    if keep_frames:
        out["frames"] = frames
    return out


def estimate(runners: list, track: Track, sims: int = ODDS_SIMS, seed: int = 0) -> tuple:
    """(勝率の並び, 3着以内率の並び)。同じ出走馬を sims 回走らせて数える。"""
    rng = random.Random(seed)
    n = len(runners)
    win, place = [0] * n, [0] * n
    for _ in range(sims):
        order = simulate(runners, track, rng)["order"]
        win[order[0]] += 1
        for i in order[:3]:
            place[i] += 1
    return [w / sims for w in win], [p / sims for p in place]


def estimate_all(runners: list, track: Track, sims: int = ODDS_SIMS, seed: int = 0) -> dict:
    """単勝・複勝と、組み合わせの馬券の確率を、1回のシミュレーションの繰り返しでまとめて数える。

    {"win": {枠: 確率}, "place": {枠: 確率}, "quinella": {キー: 確率}, "wide": …, "exacta": …, "trio": …}（キーは combo_key）。
    """
    rng = random.Random(seed)
    counts = {k: {} for k in BET_KINDS}
    for _ in range(sims):
        lanes = [runners[i].lane for i in simulate(runners, track, rng)["order"]]
        hits = [("win", str(lanes[0]))]
        hits += [("place", str(x)) for x in lanes[:3]]
        hits.append(("quinella", combo_key("quinella", lanes[:2])))
        hits.append(("exacta", combo_key("exacta", lanes[:2])))
        hits += [
            ("wide", combo_key("wide", pair))
            for pair in ((lanes[0], lanes[1]), (lanes[0], lanes[2]), (lanes[1], lanes[2]))
        ]
        hits.append(("trio", combo_key("trio", lanes[:3])))
        for kind, key in hits:
            counts[kind][key] = counts[kind].get(key, 0) + 1
    return {k: {key: n / sims for key, n in d.items()} for k, d in counts.items()}


# ---- 戦績で決めるオッズ ----
RECORD_PRIOR_RUNS = _CFG.record_prior_runs
FIRST_RUN_FACTOR = _CFG.first_run_factor
FORM_ODDS = {f.value: f.odds_mult for f in _CFG.forms}
PLACE_CAP = _CFG.place_cap


def npc_record(rng: random.Random) -> tuple:
    """帝愛の馬の、作りものの戦績 (出走数, 勝ち数, 3着以内の数)。強さとは関係なく、平均的な成績のまわりでばらつく。"""
    races = rng.randint(0, 12)
    wins = sum(rng.random() < 1 / FIELD_SIZE for _ in range(races))
    places = wins + sum(rng.random() < 2 / (FIELD_SIZE - 1) for _ in range(races - wins))
    return races, wins, places


def _win_weights(runners: list) -> list:
    """勝つ確率の見積もり（合計 1）。戦績の勝率を平均に寄せ、初出走は下げ、調子で補正する。"""
    n = len(runners)
    prior = 1 / n
    raw = []
    for r in runners:
        p = (r.wins + RECORD_PRIOR_RUNS * prior) / (r.races + RECORD_PRIOR_RUNS)
        if r.races == 0:
            p *= FIRST_RUN_FACTOR
        raw.append(p * FORM_ODDS[r.form])
    total = sum(raw)
    return [x / total for x in raw]


def _place_probs(runners: list) -> list:
    """3着以内に入る確率の見積もり（合計はほぼ 3）。"""
    n = len(runners)
    prior = min(3, n) / n
    raw = []
    for r in runners:
        p = (r.places + RECORD_PRIOR_RUNS * prior) / (r.races + RECORD_PRIOR_RUNS)
        if r.races == 0:
            p *= FIRST_RUN_FACTOR
        raw.append(p * FORM_ODDS[r.form])
    scale = min(3, n) / sum(raw)
    return [min(PLACE_CAP, x * scale) for x in raw]


def _odds(prob: float, takeout: float, cap: float) -> float:
    """確率から、控除を引いた倍率（小数1桁に切り捨て・1.0 倍以上・cap 以下）。"""
    if prob <= 0:
        return cap
    return max(1.0, min(cap, math.floor((1 - takeout) / prob * 10) / 10))


def make_all_odds(runners: list) -> dict:
    """戦績で決めた、全部の馬券のオッズ。{"win": {枠: オッズ}, "place": {枠: オッズ}, "quinella": {キー: オッズ}, …}。

    単勝は勝つ確率の見積もり、複勝は3着以内率の見積もり。組み合わせの馬券は、勝つ確率から、着順の確率（強い馬ほど先に決まる）を
    計算して出す。
    """
    lanes = [r.lane for r in runners]
    win = dict(zip(lanes, _win_weights(runners)))
    place = dict(zip(lanes, _place_probs(runners)))

    def first_two(a: int, b: int) -> float:  # a が1着・b が2着
        return win[a] * win[b] / (1 - win[a])

    def top3(a: int, b: int, c: int) -> float:  # a・b・c が、この順に1〜3着
        return first_two(a, b) * win[c] / (1 - win[a] - win[b])

    def trio(a: int, b: int, c: int) -> float:
        return sum(top3(*p) for p in itertools.permutations((a, b, c)))

    out = {
        "win": {str(x): _odds(win[x], TAKEOUT, MAX_ODDS) for x in lanes},
        "place": {str(x): _odds(place[x], TAKEOUT, MAX_ODDS) for x in lanes},
    }
    pairs = [(a, b) for a in lanes for b in lanes if a < b]
    out["quinella"] = {
        combo_key("quinella", p): _odds(first_two(*p) + first_two(p[1], p[0]), COMBO_TAKEOUT, MAX_COMBO_ODDS)
        for p in pairs
    }
    out["wide"] = {
        combo_key("wide", p): _odds(
            sum(trio(p[0], p[1], c) for c in lanes if c not in p), COMBO_TAKEOUT, MAX_COMBO_ODDS
        )
        for p in pairs
    }
    out["exacta"] = {
        combo_key("exacta", (a, b)): _odds(first_two(a, b), COMBO_TAKEOUT, MAX_COMBO_ODDS)
        for a in lanes
        for b in lanes
        if a != b
    }
    out["trio"] = {
        combo_key("trio", t): _odds(trio(*t), COMBO_TAKEOUT, MAX_COMBO_ODDS) for t in itertools.combinations(lanes, 3)
    }
    return out


def make_odds(runners: list) -> list:
    """[(単勝のオッズ, 複勝のオッズ)] を、出走馬の並びで返す。"""
    odds = make_all_odds(runners)
    return [(odds["win"][str(r.lane)], odds["place"][str(r.lane)]) for r in runners]


def payout(stake: int, odds: float) -> int:
    return int(stake * odds)


# ---- 開催の時刻と馬券の種類 ----
WEEKDAY_HOURS = _CFG.weekday_hours
WEEKEND_HOURS = _CFG.weekend_hours


def schedule_for(d: datetime.date) -> tuple:
    """その日の、定時レースの発走時刻（現地時間の時・分）。0〜9時は開催しない。"""
    hours = WEEKEND_HOURS if d.weekday() >= 5 else WEEKDAY_HOURS
    return tuple((h, 0) for h in hours)


ENTRY_OPEN_MIN = _CFG.entry_open_min
ENTRY_LEAD_MIN = _CFG.entry_lead_min
BET_LEAD_SEC = _CFG.bet_lead_sec
BET_KINDS = dict(_CFG.bet_kinds)
MULTI_KINDS = _CFG.multi_kinds
COMBO_SIZE = dict(_CFG.combo_size)
COMBO_TAKEOUT = _CFG.combo_takeout
MAX_COMBO_ODDS = _CFG.max_combo_odds


def combo_key(kind: str, lanes) -> str:
    """組み合わせの馬券の、オッズ表のキー。順不同のものは小さい順にそろえ、馬単だけ順番を残す（例: "2-5"・"5>2"）。"""
    lanes = list(lanes)
    return ">".join(map(str, lanes)) if kind == "exacta" else "-".join(map(str, sorted(lanes)))


def combo_hit(kind: str, lanes, order_lanes: list) -> bool:
    """着順（枠番の並び）で、その組み合わせが当たりか。"""
    lanes = list(lanes)
    if kind == "quinella":
        return set(lanes) == set(order_lanes[:2])
    if kind == "exacta":
        return lanes == order_lanes[:2]
    if kind == "wide":
        return set(lanes) <= set(order_lanes[:3])
    if kind == "trio":
        return set(lanes) == set(order_lanes[:3])
    return False


NPC_NAMES = list(_CFG.npc_names)
BANNED_NAME_CHARS = set(_CFG.banned_name_chars)  # メンション・リンク・装飾を防ぐ


def check_name(name: str) -> tuple:
    """馬の名前が使えるか。(使えるか, 整えた名前か理由)。メンション・リンク・装飾に使う文字は禁止。"""
    name = name.strip()
    if not name:
        return False, "名前を入れてください。"
    if len(name) > NAME_MAX:
        return False, f"名前は {NAME_MAX} 文字までです。"
    if any(c in BANNED_NAME_CHARS for c in name) or "http" in name.lower():
        return False, "名前に使えない文字（@ < > ` * _ ~ | # : / など）か、リンクが入っています。"
    return True, name
