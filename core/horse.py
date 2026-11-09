"""競馬の数字と、レースのシミュレーション。Discord と DB に依存しない純粋なロジック。

- 馬のステータスは4つ（スピード・スタミナ・パワー・根性）で、**合計は固定**（STAT_TOTAL）。強さは買えず、配分（作り）だけを選ぶ。
  にんじん1本で、CARROT_POINTS 点を、あるステータスから別のステータスへ動かせる。
- コースは、距離（短・中・長）と馬場（良・稍重・重）。短距離はスピード、長距離はスタミナ、重い馬場はパワーが効く。
- レースは、1ティックごとに全馬が進み、毎ティックの運（ノイズ）と、レース全体の運がある。結果は、シード（乱数）で決まる。
- オッズは、同じ出走馬を何千回も走らせて勝率・3着以内率を出し、控除（TAKEOUT）を引いて決める。
"""

import math
import random
from dataclasses import dataclass

STAT_KEYS = ("speed", "stamina", "power", "guts")
STAT_NAMES = {"speed": "スピード", "stamina": "スタミナ", "power": "パワー", "guts": "根性"}
STAT_TOTAL = 200  # 4つの合計（固定）
STAT_MIN, STAT_MAX = 20, 80
DEFAULT_STATS = {"speed": 50, "stamina": 50, "power": 50, "guts": 50}
CARROT_POINTS = 5  # にんじん1本で動かせるポイント

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
ODDS_SIMS = 8000  # オッズを決めるためのシミュレーション回数（多いほど、推定のぶれが小さい。別スレッドで走らせる）
ODDS_MARGIN_Z = (
    1.5  # 推定のぶれの分だけ、確率を多めに見積もる（オッズが甘くなって、買い続けると増える馬券ができるのを防ぐ）
)
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


def odds_from(prob: float, sims: int = ODDS_SIMS, floor: float = 1.0) -> float:
    """公正な倍率（1/確率）から控除を引いた倍率。小数1桁に切り捨てる（切り上げると、期待値が1を超えかねない）。

    prob は sims 回のシミュレーションで数えた割合。その誤差（標準偏差）の ODDS_MARGIN_Z 倍を足して、確率を多めに見積もる。
    """
    if prob <= 0:
        return MAX_ODDS
    prob = min(1.0, prob + ODDS_MARGIN_Z * math.sqrt(prob * (1 - prob) / sims))
    return max(floor, min(MAX_ODDS, math.floor((1 - TAKEOUT) / prob * 10) / 10))


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


def combo_odds_from(prob: float, sims: int = ODDS_SIMS) -> float:
    """組み合わせの馬券のオッズ（控除 COMBO_TAKEOUT・上限 MAX_COMBO_ODDS。確率の見積もりは odds_from と同じ考え方）。"""
    if prob <= 0:
        return MAX_COMBO_ODDS
    prob = min(
        1.0, prob + (ODDS_MARGIN_Z + 0.5) * math.sqrt(prob * (1 - prob) / sims)
    )  # 当たりにくいぶん、ぶれが大きいので多めに
    return max(1.0, min(MAX_COMBO_ODDS, math.floor((1 - COMBO_TAKEOUT) / prob * 10) / 10))


def make_all_odds(runners: list, track: Track, sims: int = ODDS_SIMS, seed: int = 0) -> dict:
    """{"win": {枠: オッズ}, "place": {枠: オッズ}, "quinella": {キー: オッズ}, …}。全部の組み合わせを持つ（当たらなかったものは上限）。"""
    probs = estimate_all(runners, track, sims, seed)
    lanes = [r.lane for r in runners]
    out = {
        "win": {str(x): odds_from(probs["win"].get(str(x), 0.0), sims) for x in lanes},
        "place": {str(x): odds_from(probs["place"].get(str(x), 0.0), sims) for x in lanes},
    }
    pairs = [(a, b) for a in lanes for b in lanes if a < b]
    out["quinella"] = {
        combo_key("quinella", p): combo_odds_from(probs["quinella"].get(combo_key("quinella", p), 0.0), sims)
        for p in pairs
    }
    out["wide"] = {
        combo_key("wide", p): combo_odds_from(probs["wide"].get(combo_key("wide", p), 0.0), sims) for p in pairs
    }
    out["exacta"] = {
        combo_key("exacta", (a, b)): combo_odds_from(probs["exacta"].get(combo_key("exacta", (a, b)), 0.0), sims)
        for a in lanes
        for b in lanes
        if a != b
    }
    trios = [(a, b, c) for a in lanes for b in lanes for c in lanes if a < b < c]
    out["trio"] = {
        combo_key("trio", t): combo_odds_from(probs["trio"].get(combo_key("trio", t), 0.0), sims) for t in trios
    }
    return out


def make_odds(runners: list, track: Track, sims: int = ODDS_SIMS, seed: int = 0) -> list:
    """[(単勝のオッズ, 複勝のオッズ)] を、出走馬の並びで返す。"""
    win, place = estimate(runners, track, sims, seed)
    return [(odds_from(w, sims), odds_from(p, sims)) for w, p in zip(win, place)]


def payout(stake: int, odds: float) -> int:
    return int(stake * odds)


# ---- 開催の時刻と馬券の種類 ----
SCHEDULE = ((12, 0), (20, 0))  # 定時レースの発走時刻（現地時間の時・分）
ENTRY_LEAD_MIN = 30  # 発走の何分前に、出走の申し込みを締め切るか（そのあと出馬表とオッズが出る）
BET_LEAD_SEC = 120  # 発走の何秒前に、馬券の発売を締め切るか
BET_KINDS = {
    "win": "単勝",  # 1着
    "place": "複勝",  # 3着以内
    "quinella": "馬連",  # 1・2着の2頭（順不同）
    "wide": "ワイド",  # 3着以内に入る2頭（順不同）
    "exacta": "馬単",  # 1着と2着（順番どおり）
    "trio": "三連複",  # 1〜3着の3頭（順不同）
}
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
