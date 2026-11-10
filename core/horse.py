"""競馬の数字と、レースのシミュレーション。Discord と DB に依存しない純粋なロジック。

- 馬のステータスは4つ（スピード・スタミナ・パワー・根性）で、**合計は固定**（STAT_TOTAL）。強さは買えず、配分（作り）だけを選ぶ。
  にんじん1本で、CARROT_POINTS 点を、あるステータスから別のステータスへ動かせる。
- コースは、距離（短・中・長）と馬場（良・稍重・重）。短距離はスピード、長距離はスタミナ、重い馬場はパワーが効く。
- レースは、1ティックごとに全馬が進み、毎ティックの運（ノイズ）と、レース全体の運がある。結果は、シード（乱数）で決まる。
- オッズは、出走馬の**戦績**（出走数・勝ち数・3着以内の数）で決める。出走が少ない馬は平均に寄せ、初出走は高めにし、調子（◎○△）で動かす。
  ステータスはオッズに入らない（実際の強さと戦績のずれが、狙い目になる）。控除（TAKEOUT）を引く。
  レースの結果は、ステータスのシミュレーションで決まる（estimate は、その本当の確率を調べる検証用）。
"""

import itertools
import math
import random
from dataclasses import dataclass

STAT_KEYS = ("speed", "stamina", "power", "guts")
STAT_NAMES = {"speed": "スピード", "stamina": "スタミナ", "power": "パワー", "guts": "根性"}
STAT_TOTAL = 200  # 4つの合計（固定）
STAT_MIN, STAT_MAX = 20, 80
DEFAULT_STATS = {"speed": 50, "stamina": 50, "power": 50, "guts": 50}
CARROT_POINTS = 5  # にんじん1本で動かせるポイント

GOLD_CARROT_POINTS = 5  # 金にんじん1本で、2か所に5点ずつ（合計10点）増やせる
GOLD_CARROT_MAX = 1  # 1頭に使える金にんじんの数（合計 +10 点まで）

FIELD_SIZE = 8  # 1レースの出走頭数
NAME_MAX = 12
MAX_HORSES = 3  # 1人が持てる馬の数

COATS = ["栗毛", "鹿毛", "黒鹿毛", "青毛", "芦毛", "白毛", "栃栗毛", "月毛"]
SILKS = ["🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "⬛", "⬜", "🟫"]  # 勝負服の色

DISTANCES = (1000, 1200, 1600, 1800, 2000, 2400, 3000)
GOINGS = ("良", "稍重", "重", "不良")
GOING_PENALTY = {"良": 0.0, "稍重": 0.012, "重": 0.03, "不良": 0.05}
GOING_WEIGHT = (60, 25, 11, 4)  # 馬場の出やすさ（良が多い）
FORM_MARK = {1: "◎", 0: "○", -1: "△"}  # 調子（毎レース、ランダム）
FORM_MULT = {1: 1.015, 0: 1.0, -1: 0.985}

TAKEOUT = 0.15  # 控除率（オッズは、公正な倍率の 1 - TAKEOUT 倍）
ODDS_SIMS = 8000  # estimate（本当の確率を調べる検証用）の、シミュレーション回数
MAX_ODDS = 99.9
MIN_STAKE, MAX_STAKE = 100, 10_000  # 1回の賭け金
MAX_RACE_STAKE = 30_000  # 1人が1レースに賭けられる合計

ENTRY_FEE = 1_000  # 出走料
PRIZES = (12_000, 5_000, 2_000)  # 1着・2着・3着の賞金（馬主へ）

TICKS = 60  # 平均的な馬が走り切るティック数
MAX_TICKS = 140


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

    def stat(self, key: str) -> int:
        return getattr(self, key)


@dataclass(frozen=True)
class Track:
    distance: int
    going: str

    @property
    def label(self) -> str:
        return f"{self.distance:,}m・{self.going}"


def valid_stats(stats: dict) -> bool:
    """合計が STAT_TOTAL ちょうどで、すべてが範囲内か。"""
    return (
        set(stats) == set(STAT_KEYS)
        and sum(stats.values()) == STAT_TOTAL
        and all(STAT_MIN <= v <= STAT_MAX for v in stats.values())
    )


RETIRE_WIN = 8_000  # 引退金: 1勝ごと
RETIRE_PLACE = 3_000  # 3着以内（勝ちを除く）1回ごと
RETIRE_LOSS = 1_500  # 3着に入れなかった1回ごとの、引退費用
RETIRE_MAX, RETIRE_FEE_MAX = 100_000, 20_000  # 引退金の上限・引退費用の上限


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


def random_stats(rng: random.Random) -> dict:
    """範囲内で、合計が STAT_TOTAL になる、ランダムな配分（NPC 用）。"""
    while True:
        raw = [rng.random() ** 0.8 + 0.25 for _ in STAT_KEYS]
        scale = (STAT_TOTAL - STAT_MIN * len(STAT_KEYS)) / sum(raw)
        vals = [STAT_MIN + int(r * scale) for r in raw]
        vals[0] += STAT_TOTAL - sum(vals)  # 端数は先頭に寄せる
        stats = dict(zip(STAT_KEYS, vals))
        if valid_stats(stats):
            return stats


def random_track(rng: random.Random) -> Track:
    return Track(rng.choice(DISTANCES), rng.choices(GOINGS, weights=GOING_WEIGHT, k=1)[0])


def random_form(rng: random.Random) -> int:
    return rng.choices([1, 0, -1], weights=[2, 5, 2], k=1)[0]


# ---- シミュレーション ---- #
def speed_coef(distance: int) -> float:
    """距離ごとの、スピードの効き（短いほど効く）。1800m で 0.05。"""
    return min(0.09, 0.05 * 1800 / distance)


FATIGUE_MAX = 0.18  # バテたときの、最大の減速
NOISE = 0.03  # 毎ティックの運
LUCK = 0.020  # レース全体の運（展開）


def _mult(r: Runner, p: float, track: Track, luck: float, spurt: float) -> float:
    dfac = track.distance / 1800
    m = 1 + (r.speed - 50) / 50 * speed_coef(track.distance)
    m *= 1 + (r.power - 50) / 50 * 0.03 * max(0.0, 1 - p / 0.2)  # 出足
    reach = min(1.0, max(0.25, 0.8 * (r.stamina / 50) / dfac))  # ここまでは、バテずに走れる
    if p > reach and reach < 1.0:
        m *= 1 - FATIGUE_MAX * (p - reach) / (1 - reach)
    if p >= 0.75:
        m *= 1 + spurt  # ラストスパート
    m *= 1 - GOING_PENALTY[track.going] * (1 - (r.power - 20) / 60 * 0.7)  # 馬場
    return m * FORM_MULT[r.form] * (1 + luck)


def simulate(runners: list, track: Track, rng: random.Random, keep_frames: bool = False) -> dict:
    """1レース走らせる。{"order": 着順（lane の並び）, "times": 着順ごとのゴール時刻, "frames": ティックごとの進み（keep_frames のとき）}。"""
    n = len(runners)
    pos = [0.0] * n
    finish = [None] * n
    luck = [rng.gauss(0, LUCK) for _ in runners]
    spurt = [max(0.0, (r.guts - 20) / 60 * 0.05 * rng.uniform(0.5, 1.5)) for r in runners]
    frames = [[0.0] * n] if keep_frames else None
    for t in range(1, MAX_TICKS + 1):
        for i, r in enumerate(runners):
            if finish[i] is not None:
                continue
            step = _mult(r, pos[i], track, luck[i], spurt[i]) * (1 + rng.gauss(0, NOISE)) / TICKS
            new = pos[i] + max(0.0, step)
            if new >= 1.0:
                finish[i] = t - 1 + (1.0 - pos[i]) / max(1e-9, new - pos[i])  # ゴールを越えた時刻（ティック内で補間）
                new = 1.0
            pos[i] = new
        if keep_frames:
            frames.append(list(pos))
        if all(f is not None for f in finish):
            break
    for i in range(n):
        if finish[i] is None:
            finish[i] = MAX_TICKS + (1 - pos[i])
    order = sorted(range(n), key=lambda i: (finish[i], rng.random()))
    out = {"order": order, "times": [finish[i] for i in order], "finish": finish}
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
RECORD_PRIOR_RUNS = (
    4  # 戦績を信じる強さ（この回数ぶんの「平均的な成績」を足して薄める。出走が少ない馬ほど、平均に寄る）
)
FIRST_RUN_FACTOR = 0.75  # 初出走の馬は、確率をこの倍率で見積もる（オッズが高くなる）
FORM_ODDS = {1: 1.15, 0: 1.0, -1: 0.85}  # 調子による、確率の補正（◎ は低く、△ は高くなる）
PLACE_CAP = 0.95  # 3着以内率の上限


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
SCHEDULE = (
    (9, 0),
    (13, 0),
    (17, 0),
    (21, 0),
)  # 定時レースの発走時刻（現地時間の時・分）。9時から0時までの間に、4時間おきで1日4回
ENTRY_OPEN_MIN = 120  # 発走の何分前に、出走の申し込みを始めるか
ENTRY_LEAD_MIN = 60  # 発走の何分前に、出走の申し込みを締め切るか（そのあと出馬表とオッズが出る）
# 1回のレースは4時間の周期: 申し込み1時間（発走の2〜1時間前）→ 出馬表・馬券1時間（発走の1時間前〜）→ 発走・結果（次の申し込みまで2時間）
BET_LEAD_SEC = 120  # 発走の何秒前に、馬券の発売を締め切るか
BET_KINDS = {
    "win": "単勝",  # 1着
    "place": "複勝",  # 3着以内
    "quinella": "馬連",  # 1・2着の2頭（順不同）
    "wide": "ワイド",  # 3着以内に入る2頭（順不同）
    "exacta": "馬単",  # 1着と2着（順番どおり）
    "trio": "三連複",  # 1〜3着の3頭（順不同）
}
MULTI_KINDS = ("win", "place")  # 1頭ずつの馬券。画面では、何頭でもまとめて選べる（同じ金額ずつ、別々の馬券として買う）
COMBO_SIZE = {"quinella": 2, "wide": 2, "exacta": 2, "trio": 3}  # 組み合わせの馬券で選ぶ頭数
COMBO_TAKEOUT = 0.20  # 組み合わせの馬券の控除率（当たりにくいぶん、控除は大きい）
MAX_COMBO_ODDS = 100.0  # 組み合わせの馬券のオッズの上限（1発の払い戻しが大きくなりすぎない）


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


NPC_NAMES = [
    "ハヤテノツバサ",
    "ミッドナイトブルー",
    "サクラフブキ",
    "ゴールデンキャロット",
    "カミナリボルト",
    "ダイヤモンドロード",
    "ナイトメアランナー",
    "ツキノヒカリ",
    "アカツキレッド",
    "シルバーアロー",
    "ドリームチェイサー",
    "イナズマライダー",
    "ブラックサンダー",
    "ハルノオトメ",
    "ワイルドハート",
    "ロイヤルクラウン",
    "キタノカゼ",
    "スターライトダッシュ",
    "ホワイトファング",
    "ユメノツヅキ",
    "ライジングサン",
    "オオゾラノキセキ",
    "ジェットストリーム",
    "ミラクルバンブー",
    "ブルーコメット",
    "アサヒノプリンス",
    "テンカイチ",
    "グランドフィナーレ",
    "ヒバナノゴトク",
    "カゲロウ",
]
BANNED_NAME_CHARS = set("@<>`*_~|\\\n\r\t#:/")  # メンション・リンク・装飾を防ぐ


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
