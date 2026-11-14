"""地下作業（クリッカー）の数字と計算。DB にも Discord にも触らない（処理は core/eco_labor.py、画面は cogs/labor_cog.py）。

ペリカが尽きた人の救済。元手は 0。「働く」ボタンを押すとペリカが貯まる。
- 疲労: 押すたびに溜まり、溜まるほど1回の稼ぎが減る。時間で自然に回復する（事故も、休憩もない）
- 工具: ツルハシ（稼ぎ）・手袋（疲労の溜まりにくさ）を、稼いだペリカで強化する
「働く」クリックには、1日の押せる回数の上限がある（MAX_CLICKS_PER_DAY）。手元の制限はない。稼ぎの速さは、疲労で自然に頭打ちになる。
作業員は、1日の契約で雇う（自動更新）。作業員の稼ぎだけでは契約料に届かず（赤字）、働いた1時間ごとに鉱石を掘り、
レア鉱石を引けば大きく黒字になる。期待値は、契約料の100%未満（運の勝負）。1日で結果が分かる。
"""

import math
import random

TOOLS = ("pick", "glove")
TOOL_NAME = {"pick": "ツルハシ", "glove": "手袋"}
TOOL_MARK = {"pick": "⛏", "glove": "🧤"}
TOOL_EFFECT = {"pick": "稼ぎが増える", "glove": "疲れにくくなる"}
MAX_LEVEL = 10
UPGRADE_COST = (
    150,
    400,
    900,
    1_800,
    3_500,
    7_000,
    14_000,
    28_000,
    56_000,
    112_000,
)  # 次のレベルへ上げる値段（レベル0→1, 1→2, …）。ほぼ倍々

MAX_CLICKS_PER_DAY = 1_000  # 「働く」を押せる、1日の回数の上限（作業員の稼ぎ・鉱石は対象外）。超えた分は、1回あたり OVER_CAP_PAY ペリカしか稼げない
# 回数で区切るので、ツルハシを強化するほど、1日に稼げる額もそのまま増える（ペリカの額で区切ると、上限に届くのが早くなるだけになってしまう）
OVER_CAP_PAY = 1

BASE_PAY = 10  # 工具なしの、1回の稼ぎ
PAY_PER_LEVEL = 4  # ツルハシ1段ごとの加算（Lv10 で最大 50）
FATIGUE_MAX = 100.0
FATIGUE_GAIN = 3.0  # 工具なしの、1回で溜まる疲労
GAIN_CUT_PER_LEVEL = 0.4  # 手袋1段ごとの軽減（Lv5 まで。Lv5 で 1.0）
LATE_GAIN_CUT_PER_LEVEL = 0.1  # Lv6 以降の軽減（0 や負にならないよう、小さくする。Lv10 で 2.0）
EARLY_LEVELS = 5
MIN_PAY_RATE = 0.2  # 疲労が満タンのときの、稼ぎの割合（空のときは 1.0）
RECOVER_PER_SEC = 0.1  # 疲労の自然回復（100 回復するのに約17分）

DRINK_RECOVER = 60.0  # 栄養ドリンコ（ショップ）1本で回復する疲労
CLICK_INTERVAL = 1.0  # 連打の最短間隔（秒）


def clamp_level(level: int) -> int:
    return max(0, min(MAX_LEVEL, int(level)))


def pay_per_click(pick: int) -> int:
    return BASE_PAY + PAY_PER_LEVEL * clamp_level(pick)


def fatigue_gain(glove: int) -> float:
    level = clamp_level(glove)
    early = min(level, EARLY_LEVELS)
    return FATIGUE_GAIN - GAIN_CUT_PER_LEVEL * early - LATE_GAIN_CUT_PER_LEVEL * (level - early)


def fatigue_now(fatigue: float, since: float, now: float, recovery: float = 0.0) -> float:
    """最後に疲労を更新してから時間がたった、いまの疲労。recovery は、回復の速さの上がり幅（発掘品の効果。0.01 で +1%）。"""
    return max(0.0, min(FATIGUE_MAX, fatigue - max(0.0, now - since) * RECOVER_PER_SEC * (1 + recovery)))


def pay_rate(fatigue: float) -> float:
    """疲労に応じた、稼ぎの割合（1.0 〜 MIN_PAY_RATE）。"""
    return 1.0 - (1.0 - MIN_PAY_RATE) * max(0.0, min(FATIGUE_MAX, fatigue)) / FATIGUE_MAX


def click_pay(pick: int, fatigue: float) -> int:
    """いまの疲労での、1回の稼ぎ。少なくとも 1。"""
    return max(1, round(pay_per_click(pick) * pay_rate(fatigue)))


def upgrade_cost(level: int):
    """いまのレベルから次へ上げる値段。最大なら None。"""
    level = clamp_level(level)
    return None if level >= MAX_LEVEL else UPGRADE_COST[level]


def upgrade_spent(level: int) -> int:
    """いまのレベルに上げるまでに、これまで払った額の合計（資産の計算用）。"""
    return sum(UPGRADE_COST[: clamp_level(level)])


def bar(fatigue: float, width: int = 10) -> str:
    n = round(width * max(0.0, min(FATIGUE_MAX, fatigue)) / FATIGUE_MAX)
    return "🟥" * n + "⬜" * (width - n)


def click_outcome(pick: int, glove: int, fatigue: float) -> dict:
    """1回押した結果（残高などは見ない）。{pay, fatigue}。fatigue は押したあとの疲労。"""
    return {"pay": click_pay(pick, fatigue), "fatigue": min(FATIGUE_MAX, fatigue + fatigue_gain(glove))}


# ---------------------------------------------------------------- #
# 鉱脈ヒット・作業コンボ・今日の地層（手掘りを気持ちよくする、見習い〜職人の全員に効く）
# ---------------------------------------------------------------- #
CRIT_BASE = 0.05  # 鉱脈ヒットの確率（ツルハシLv0）
CRIT_PER_LEVEL = 0.01  # ツルハシ1Lvごとに上乗せ（Lv10で+10%）
CRIT_WORKER_BONUS = 0.002  # 雇用中の作業員1人ごとに上乗せ（現場シナジー）
CRIT_MAX = 0.30  # 鉱脈ヒット率の上限
CRIT_MULT = 5  # 鉱脈ヒットの稼ぎの倍率
CRIT_ORE_CHANCE = 0.15  # 鉱脈ヒットのとき、さらに鉱石も掘り当てる確率
# 手掘り用の小さな鉱石表（ヒットの中での相対比。ダイヤ・伝説級は出ない。(id, 名前, 印, 相対比, 売値)）
CRIT_ORES = (
    ("coal", "石炭", "⚫", 45, 3),
    ("copper", "銅", "🟤", 40, 60),
    ("silver", "銀", "⚪", 12, 240),
    ("gold", "金", "🟡", 3, 1_200),
)

COMBO_SIZE = 10  # 「働く」が、この回数に届くごとに
COMBO_BONUS = 0.20  # コンボ達成時の、その1回の稼ぎの上乗せ（+20%）

TERRAIN_KINDS = ("hit", "fatigue", "silver", "none")
TERRAIN_HIT_BONUS = 0.05  # 鉱脈ヒット率 +5%
TERRAIN_FATIGUE_CUT = 0.20  # 疲労の溜まりが20%軽くなる
TERRAIN_SILVER_MULT = 2  # 手掘りの銀の出やすさが2倍


def terrain_today(day: str) -> str:
    """日替わりの「今日の地層」（サーバー・人を問わず、日付だけで決まる）。TERRAIN_KINDS のどれか。"""
    return random.Random(f"terrain:{day}").choice(TERRAIN_KINDS)


def terrain_label(kind: str) -> str:
    return {
        "hit": f"⛏ 鉱脈ヒット率 +{TERRAIN_HIT_BONUS:.0%}",
        "fatigue": f"😌 疲労の溜まりが{TERRAIN_FATIGUE_CUT:.0%}軽い",
        "silver": f"⚪ 銀が{TERRAIN_SILVER_MULT}倍出やすい（手掘り）",
        "none": "特になし",
    }[kind]


def crit_chance(pick: int, workers: int, terrain: str) -> float:
    """鉱脈ヒットの確率。ツルハシのレベルと、雇用中の作業員の数で上がる。"""
    c = CRIT_BASE + CRIT_PER_LEVEL * clamp_level(pick) + CRIT_WORKER_BONUS * max(0, workers)
    if terrain == "hit":
        c += TERRAIN_HIT_BONUS
    return min(CRIT_MAX, c)


def roll_crit_ore(rng, terrain: str) -> str:
    """鉱脈ヒットで、さらに鉱石も出たときの、その鉱石のID。"""
    weights = [
        w * (TERRAIN_SILVER_MULT if terrain == "silver" and cid == "silver" else 1) for cid, _, _, w, _ in CRIT_ORES
    ]
    return rng.choices([cid for cid, _, _, _, _ in CRIT_ORES], weights=weights, k=1)[0]


# ---------------------------------------------------------------- #
# 雇用（作業員）。放置でも稼げる。数字はここだけを直せばよい
# ---------------------------------------------------------------- #
WORKERS = ("apprentice", "miner", "craftsman", "foreman")
WORKER_NAME = {
    "apprentice": "見習い",
    "miner": "坑夫",
    "craftsman": "職人",
    "foreman": "監督",
}
WORKER_MARK = {"apprentice": "🧒", "miner": "👷", "craftsman": "🧑‍🔧", "foreman": "🕴️"}
WORKER_RATE = {
    "apprentice": 1.8,
    "miner": 10.05,
    "craftsman": 23.55,
    "foreman": 0,
}  # 作業員の稼ぎ（1時間）。毎日受け取る人の、契約料の約3割（監督は掘らない）
CONTRACT_DAYS = 1  # 契約の期間（日）。切れたら、自動更新（オンの場合）か、退職。1日で、勝ち負けが分かる
# 契約料（雇うときと、更新のときに払う）。毎日受け取る人が、契約の1日（24時間）に稼ぐ、作業員の稼ぎ（約3割）と鉱石の期待値（約5割）の合計を上回る
CONTRACT_FEE = {"apprentice": 150, "miner": 520, "craftsman": 1_160, "foreman": 640}
FOREMAN_LUCK = 0.25  # 監督1人ごとに、レア鉱石の出やすさが増える割合（作業員の稼ぎは増えない）

COAL_P = 0.40  # 石炭が出る確率（働いた1時間ごと。見習いでも、半分近い時間で何か掘れる）

# 鉱石。働いた1時間ごとに1回、掘る（外れもある）。(id, 名前, 印, 出る確率, 売値)。確率は、ORE_POWER=1 の作業員のもの
ORES = (
    ("copper", "銅", "🟤", 0.06, 60),
    ("silver", "銀", "⚪", 0.015, 240),
    ("gold", "金", "🟡", 0.003, 1_200),
    ("diamond", "ダイヤ", "💎", 0.0004, 9_000),
    ("legend", "伝説の鉱石", "🌟", 0.00004, 90_000),
    (
        "coal",
        "石炭",
        "⚫",
        COAL_P,
        3,
    ),  # ありふれた鉱石。出やすさは、作業員の種類・監督に関係なく同じ（鉱石が全く出ない、をなくす）
)
FLAT = ("coal",)  # 出やすさに ORE_POWER・監督の運をかけない鉱石
ORE_NAME = {o[0]: o[1] for o in ORES}
ORE_MARK = {o[0]: o[2] for o in ORES}
ORE_VALUE = {o[0]: o[4] for o in ORES}
ORE_BIG = ("diamond", "legend")  # 公開で知らせる当たり
# 作業員の種類ごとの、鉱石の出やすさ（確率にかける。契約料に比例して、期待値は契約料の約3.5割）
ORE_POWER = {"apprentice": 0.12, "miner": 0.415, "craftsman": 0.95, "foreman": 0.0}

# 発掘品（コレクション用の宝石・化石・遺物）。ふつうの作業員が、鉱石を掘るときに、まれに見つける（ペリカにはならず、持ち物になる）。
# 売値は決まっていない。競売の画面（ショップの「🕴️ 帝愛の競売」）で、プレイヤーどうしが売り買いする。value は、取引の値段の幅の目安（定価の0.1〜5倍。同じ2人の取引の上限は10万）
# (id, 名前, 印, 出る確率（働いた1時間ごと。RELIC_POWER=1 の作業員のもの）, 目安の価値)。ペリカを新しく作らない（買い手が払う）
RELICS = (
    ("fossil", "三葉虫の化石", "🐚", 0.0033, 1_500),
    ("bone", "恐竜の骨", "🦴", 0.0017, 4_000),
    ("meteor", "隕石の欠片", "☄️", 0.00067, 10_000),
    ("vase", "古代文明の壺", "🏺", 0.00027, 25_000),
    ("crown", "王家の黄金冠", "👑", 0.000067, 60_000),
    ("tear", "竜の涙", "🔮", 0.000017, 100_000),
)
RELIC_ID = {r[0]: f"relic_{r[0]}" for r in RELICS}  # ショップの品のID（core/shop.py）
RELIC_BIG = ("crown", "tear")  # 公開で知らせる当たり
# 作業員の種類ごとの、発掘品の出やすさ（確率にかける。上位の作業員ほど出やすい。監督の運も効く。監督は掘らない）
RELIC_POWER = {"apprentice": 0.5, "miner": 0.8, "craftsman": 1.0, "foreman": 0.0}

SLOTS_BASE = 5  # 雇える人数（監督も数える）
DORM_COST = (2_000, 6_000, 15_000, 40_000, 100_000)  # 寮を1段上げる値段。1段ごとに +1人
DORM_MAX = 5

STOCK_HOURS = 24.0  # 受け取らずに貯められる時間（超えた分は消える）


def contract_fee(kind: str) -> int:
    """契約料（雇うとき・更新のとき）。"""
    return CONTRACT_FEE[kind]


def slots(dorm: int) -> int:
    return SLOTS_BASE + clamp_dorm(dorm)


def clamp_dorm(level: int) -> int:
    return max(0, min(DORM_MAX, int(level)))


def dorm_cost(level: int):
    """寮を次へ上げる値段。最大なら None。"""
    level = clamp_dorm(level)
    return None if level >= DORM_MAX else DORM_COST[level]


def dorm_spent(level: int) -> int:
    """いまの寮の段に上げるまでに、これまで払った額の合計（資産の計算用）。"""
    return sum(DORM_COST[: clamp_dorm(level)])


def foreman_luck(foremen: int) -> float:
    return FOREMAN_LUCK * max(0, foremen)


def idle_rate(counts: dict) -> float:
    """作業員たちの、1時間あたりの稼ぎ（ペリカ。鉱石は含まない）。counts は {種類: 人数}。"""
    return sum(WORKER_RATE[k] * counts.get(k, 0) for k in WORKERS)


def ore_ev(kind: str, luck: float = 0.0) -> float:
    """作業員1人の、1時間あたりの鉱石の期待値（ペリカ）。"""
    if ORE_POWER[kind] <= 0:
        return 0.0
    return sum(p * v * (1 if oid in FLAT else ORE_POWER[kind] * (1 + luck)) for oid, _, _, p, v in ORES)


def roll_ores(kind: str, hours: float, luck: float, rng) -> dict:
    """作業員1人が hours 時間ぶん働いて掘った鉱石 {id: 個数}。端数の時間は、確率で1回にする。"""
    n = int(hours)
    if hours > n and rng.random() < hours - n:
        n += 1
    scale = ORE_POWER[kind] * (1 + luck)
    found: dict = {}
    if ORE_POWER[kind] <= 0:  # 監督は掘らない
        return found
    for _ in range(n):
        x = rng.random()
        for oid, _, _, p, _ in ORES:  # 小さい確率の区間を順に重ねる（石炭は、最後）
            x -= p * (1 if oid in FLAT else scale)
            if x < 0:
                found[oid] = found.get(oid, 0) + 1
                break
    return found


def roll_relics(kind: str, hours: float, luck: float, rng) -> dict:
    """作業員1人が hours 時間ぶん働いて見つけた発掘品 {id: 個数}。端数の時間は、確率で1回にする。監督の運が効く（監督は探さない）。"""
    power = RELIC_POWER.get(kind, 0.0)
    found: dict = {}
    if power <= 0:
        return found
    n = int(hours)
    if hours > n and rng.random() < hours - n:
        n += 1
    scale = power * (1 + luck)
    for _ in range(n):
        x = rng.random()
        for rid, _, _, p, _ in RELICS:
            x -= p * scale
            if x < 0:
                found[rid] = found.get(rid, 0) + 1
                break
    return found


def relic_ev(kind: str, luck: float = 0.0) -> float:
    """作業員1人の、1時間あたりの発掘品の価値の期待値（目安の価値で数える）。"""
    return sum(p * v * RELIC_POWER.get(kind, 0.0) * (1 + luck) for _, _, _, p, v in RELICS)


def ore_gain(found: dict) -> int:
    return sum(ORE_VALUE[k] * n for k, n in found.items())


def idle_hours(since: float, now: float) -> float:
    """最後に受け取ってから、稼ぎに数える時間（STOCK_HOURS まで）。"""
    return max(0.0, min(STOCK_HOURS, (now - since) / 3600.0))


def settle_plan(
    workers: list,
    renew: bool,
    idle_at: float,
    now: float,
    pot: int,
    balance: int,
    rng=None,
    wage_bonus: float = 0.0,
    luck_bonus: float = 0.0,
) -> dict:
    """作業員の稼ぎと契約の決算（DB に触らない）。workers は [{id, kind, expires_at}]。

    稼ぎに数える時間は、最後に数えてから（STOCK_HOURS まで）。契約が切れた人は、
    - 自動更新オンで、契約料が払えれば、更新する（貯まった稼ぎから先に払い、足りなければ手元から）。新しい期限は、いまから CONTRACT_DAYS 日後
    - 払えない・オフなら、退職する（切れるまでの分だけ稼ぐ）
    作業員は、働いた1時間ごとに鉱石を掘る（監督がいるほど、レア鉱石が出やすい）。売値は、gain に入る。
    {gain, ores: {鉱石: 個数}, finds: [(時刻, 作業員の種類, 鉱石, 個数)], pot_spent, balance_spent, renewed: [(id, 新しい期限)], removed: [id]} を返す。
    rng は、random() を持つもの（鉱石・発掘品の抽選。省略なら本物の乱数）。
    """
    rng = rng or random.SystemRandom()
    hours = idle_hours(idle_at, now) if idle_at > 0 else 0.0
    start = now - hours * 3600
    active = [w for w in workers if w["expires_at"] > now]
    expired = sorted((w for w in workers if w["expires_at"] <= now), key=lambda w: -CONTRACT_FEE[w["kind"]])

    def counts_of(ws):
        c = {k: 0 for k in WORKERS}
        for w in ws:
            c[w["kind"]] += 1
        return c

    avail_pot = pot + int(
        idle_rate(counts_of(active)) * (1 + wage_bonus) * hours
    )  # 契約が続く人の稼ぎが入った、貯まった稼ぎ（鉱石は、決算の前は数えない）
    pot_spent = balance_spent = 0
    renewed, removed = [], []
    for w in expired:
        fee = CONTRACT_FEE[w["kind"]]
        if renew:
            from_pot = min(avail_pot, fee)
            if fee - from_pot <= balance - balance_spent:
                avail_pot -= from_pot
                pot_spent += from_pot
                balance_spent += fee - from_pot
                renewed.append((w["id"], now + CONTRACT_DAYS * 86400.0))
                continue
        removed.append(w["id"])
    renewed_ids = {i for i, _ in renewed}
    staying = active + [w for w in expired if w["id"] in renewed_ids]
    gain = idle_rate(counts_of(staying)) * (1 + wage_bonus) * hours
    luck = foreman_luck(counts_of(staying)["foreman"]) + luck_bonus  # 発掘品（隕石の欠片）の効果が、出やすさに足される
    ores: dict = {}
    relics: dict = {}  # 作業員が見つけた発掘品 {ショップの品のID: 個数}
    finds: list = []  # 掘り出しの履歴 [(時刻, 作業員の種類, 鉱石か発掘品のID, 個数)]。抽選は1時間ごとなので、時刻は、その時間枠の終わりにそろえる

    def dig(kind, h, begin):
        slots = max(1, math.ceil(h))
        for k, n in roll_ores(kind, h, luck, rng).items():
            ores[k] = ores.get(k, 0) + n
            for _ in range(n):
                at = min(now, begin + (1 + int(rng.random() * slots)) * 3600.0)
                finds.append((at, kind, k, 1))
        for k, n in roll_relics(kind, h, luck, rng).items():
            item_id = RELIC_ID[k]
            relics[item_id] = relics.get(item_id, 0) + n
            for _ in range(n):
                at = min(now, begin + (1 + int(rng.random() * slots)) * 3600.0)
                finds.append((at, kind, item_id, 1))

    for w in staying:
        dig(w["kind"], hours, start)
    for w in expired:  # 退職する人は、切れるまでの分だけ働く
        if w["id"] in removed:
            worked = max(0.0, min(now, w["expires_at"]) - start) / 3600.0
            gain += WORKER_RATE[w["kind"]] * (1 + wage_bonus) * worked
            dig(w["kind"], worked, start)
    return {
        "gain": int(gain) + ore_gain(ores),
        "ores": ores,
        "relics": relics,
        "finds": finds,
        "pot_spent": pot_spent,
        "balance_spent": balance_spent,
        "renewed": renewed,
        "removed": removed,
    }


# ---- 自動操作の疑いを確かめる（持ち主用の記録の集計。決めつけず、数字を見せるだけ）----
CLICK_GAP_MAX = 600.0  # 前の押下からこれ以内なら、「続けて押している」間隔として数える（秒）

# 自動操作（BOTなど）の疑いがあるときの、早めの打ち止め。BANではなく、その日はそこで止まるだけ（翌日には戻る）
# 「機械的に規則正しい」の基準は、運営の「クリック履歴」画面（cogs/_admin_embeds.py）と共通
MECH_CV_MAX = 0.1  # 間隔のばらつき（標準偏差 ÷ 平均）がこれ未満なら、機械的に規則正しいとみなす
MECH_MIN_CLICKS = 100  # 1時間にこれ未満しか押していなければ、ばらつきでは判定しない（サンプルが少なすぎる）
SUSPECT_HOURS = 3  # 直近、これだけ連続した「完了した時間」が、ずっと機械的に規則正しければ、自動操作とみなす


def is_mechanical_hour(h: dict) -> bool:
    """1時間の記録（labor_click_hours() の1行）が、機械的に規則正しいか。"""
    if h["gaps"] < 2 or h["clicks"] < MECH_MIN_CLICKS:
        return False
    mean = h["sum_gap"] / h["gaps"]
    if not mean:
        return False
    var = max(0.0, h["sum_gap2"] / h["gaps"] - mean * mean)
    return math.sqrt(var) / mean < MECH_CV_MAX


def auto_click_suspected(hour_rows: list, current_hour: int) -> bool:
    """直近 SUSPECT_HOURS 時間（いまの時間は、まだ集計中なので含めない）、ずっと機械的に規則正しければ True。

    hour_rows は、core/eco_labor.py の labor_click_hours() の結果（{hour, clicks, gaps, sum_gap, sum_gap2, ...} の並び）。
    """
    by_hour = {r["hour"]: r for r in hour_rows}
    watch = range(current_hour - SUSPECT_HOURS, current_hour)
    return all(h in by_hour and is_mechanical_hour(by_hour[h]) for h in watch)


def click_report(hours: list) -> dict:
    """1時間ごとの記録 [{hour, clicks, pay, gaps, sum_gap, sum_gap2, min_gap}]（古い順）を、画面用に集計する。

    {clicks, pay, active_hours（押した時間の数）, longest_run（休まず押し続けた連続の時間数）, rows: [{hour, clicks, pay, mean_gap, cv, min_gap}]}。
    cv（変動係数）= 間隔の標準偏差 ÷ 平均。人間が手で押すと、ふつう 0.3 以上にばらつく。0.1 を下回るのは、機械的な規則正しさ。
    """
    rows = []
    for h in hours:
        mean = h["sum_gap"] / h["gaps"] if h["gaps"] else None
        cv = None
        if mean and h["gaps"] >= 2:
            var = max(0.0, h["sum_gap2"] / h["gaps"] - mean * mean)
            cv = math.sqrt(var) / mean
        rows.append(
            {
                "hour": h["hour"],
                "clicks": h["clicks"],
                "pay": h["pay"],
                "mean_gap": mean,
                "cv": cv,
                "min_gap": h["min_gap"],
            }
        )
    longest = run = 0
    prev = None
    for r in rows:
        run = run + 1 if prev is not None and r["hour"] == prev + 1 else 1
        longest = max(longest, run)
        prev = r["hour"]
    return {
        "clicks": sum(r["clicks"] for r in rows),
        "pay": sum(r["pay"] for r in rows),
        "active_hours": len(rows),
        "longest_run": longest,
        "rows": rows,
    }
