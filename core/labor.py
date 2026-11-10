"""地下作業（クリッカー）の数字と計算。DB にも Discord にも触らない（処理は core/eco_labor.py、画面は cogs/labor_cog.py）。

ペリカが尽きた人の救済。元手は 0。「働く」ボタンを押すとペリカが貯まる。
- 疲労: 押すたびに溜まり、溜まるほど1回の稼ぎが減る。時間で自然に回復する（事故も、休憩もない）
- 工具: ツルハシ（稼ぎ）・手袋（疲労の溜まりにくさ）を、稼いだペリカで強化する
1日の上限や、手元の制限はない（連打は、手間がかかるので、続かない）。稼ぎの速さは、疲労で自然に頭打ちになる。
作業員は、7日の契約で雇う（自動更新）。基本給だけでは契約料に届かず（赤字）、働いた1時間ごとに鉱石を掘り、
レア鉱石を引けば大きく黒字になる。期待値は、契約料の100%未満（運の勝負）。
"""

import random

TOOLS = ("pick", "glove")
TOOL_NAME = {"pick": "ツルハシ", "glove": "手袋"}
TOOL_MARK = {"pick": "⛏", "glove": "🧤"}
TOOL_EFFECT = {"pick": "稼ぎが増える", "glove": "疲れにくくなる"}
MAX_LEVEL = 5
UPGRADE_COST = (150, 400, 900, 1_800, 3_500)  # 次のレベルへ上げる値段（レベル0→1, 1→2, …）

BASE_PAY = 10  # 工具なしの、1回の稼ぎ
PAY_PER_LEVEL = 4  # ツルハシ1段ごとの加算（最大 30）
FATIGUE_MAX = 100.0
FATIGUE_GAIN = 3.0  # 工具なしの、1回で溜まる疲労
GAIN_CUT_PER_LEVEL = 0.2  # 手袋1段ごとの軽減（5段で 1.0。疲労の溜まりは 3.0 → 2.0）
MIN_PAY_RATE = 0.2  # 疲労が満タンのときの、稼ぎの割合（空のときは 1.0）
RECOVER_PER_SEC = 0.2  # 疲労の自然回復（100 回復するのに約8分）

DRINK_RECOVER = 60.0  # 栄養ドリンコ（ショップ）1本で回復する疲労
CLICK_INTERVAL = 1.0  # 連打の最短間隔（秒）


def clamp_level(level: int) -> int:
    return max(0, min(MAX_LEVEL, int(level)))


def pay_per_click(pick: int) -> int:
    return BASE_PAY + PAY_PER_LEVEL * clamp_level(pick)


def fatigue_gain(glove: int) -> float:
    return FATIGUE_GAIN - GAIN_CUT_PER_LEVEL * clamp_level(glove)


def fatigue_now(fatigue: float, since: float, now: float) -> float:
    """最後に疲労を更新してから時間がたった、いまの疲労。"""
    return max(0.0, min(FATIGUE_MAX, fatigue - max(0.0, now - since) * RECOVER_PER_SEC))


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


def bar(fatigue: float, width: int = 10) -> str:
    n = round(width * max(0.0, min(FATIGUE_MAX, fatigue)) / FATIGUE_MAX)
    return "🟥" * n + "⬜" * (width - n)


def click_outcome(pick: int, glove: int, fatigue: float) -> dict:
    """1回押した結果（残高などは見ない）。{pay, fatigue}。fatigue は押したあとの疲労。"""
    return {"pay": click_pay(pick, fatigue), "fatigue": min(FATIGUE_MAX, fatigue + fatigue_gain(glove))}


# ---------------------------------------------------------------- #
# 雇用（作業員）。放置でも稼げる。数字はここだけを直せばよい
# ---------------------------------------------------------------- #
WORKERS = ("apprentice", "miner", "craftsman", "foreman")
WORKER_NAME = {"apprentice": "見習い", "miner": "坑夫", "craftsman": "職人", "foreman": "監督"}
WORKER_MARK = {"apprentice": "🧒", "miner": "👷", "craftsman": "🧑‍🔧", "foreman": "🕴️"}
WORKER_RATE = {
    "apprentice": 1.0,
    "miner": 3.75,
    "craftsman": 8.25,
    "foreman": 0,
}  # 基本給（1時間）。毎日受け取る人の、契約料の約5割（監督は掘らない）
CONTRACT_DAYS = 7  # 契約の期間（日）。切れたら、自動更新（オンの場合）か、退職
# 契約料（雇うときと、更新のときに払う）。毎日受け取る人が、契約の1週間（168時間）に稼ぐ、基本給（約5割）と鉱石の期待値（約3.5割）の合計を上回る
CONTRACT_FEE = {"apprentice": 340, "miner": 1_200, "craftsman": 2_700, "foreman": 1_500}
FOREMAN_LUCK = 0.25  # 監督1人ごとに、レア鉱石の出やすさが増える割合（基本給は増えない）

# 鉱石。働いた1時間ごとに1回、掘る（外れもある）。(id, 名前, 印, 出る確率, 売値)。確率は、ORE_POWER=1 の作業員のもの
ORES = (
    ("copper", "銅", "🟤", 0.06, 20),
    ("silver", "銀", "⚪", 0.015, 80),
    ("gold", "金", "🟡", 0.003, 400),
    ("diamond", "ダイヤ", "💎", 0.0004, 3_000),
    ("legend", "伝説の鉱石", "🌟", 0.00004, 30_000),
)
ORE_NAME = {o[0]: o[1] for o in ORES}
ORE_MARK = {o[0]: o[2] for o in ORES}
ORE_VALUE = {o[0]: o[4] for o in ORES}
ORE_BIG = ("diamond", "legend")  # 公開で知らせる当たり
# 作業員の種類ごとの、鉱石の出やすさ（確率にかける。契約料に比例して、期待値は契約料の約3.5割）
ORE_POWER = {"apprentice": 0.12, "miner": 0.415, "craftsman": 0.95, "foreman": 0.0}

SLOTS_BASE = 5  # 雇える人数（監督も数える）
DORM_COST = (2_000, 6_000, 15_000, 40_000, 100_000)  # 寮を1段上げる値段。1段ごとに +1人
DORM_MAX = 5

STOCK_HOURS = 24.0  # 受け取らずに貯められる時間（超えた分は消える）
WAGES = ("normal", "low")
WAGE_NAME = {"normal": "標準", "low": "低賃金"}
WAGE_MULT = {"normal": 1.0, "low": 1.2}  # 低賃金は、基本給が増える代わりに、作業員が辞める（鉱石は変わらない）
QUIT_PER_DAY = 0.05  # 低賃金のとき、作業員1人が1日に辞める確率


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


def foreman_luck(foremen: int) -> float:
    return FOREMAN_LUCK * max(0, foremen)


def idle_rate(counts: dict, wage: str) -> float:
    """作業員たちの、1時間あたりの基本給（ペリカ。鉱石は含まない）。counts は {種類: 人数}。"""
    return sum(WORKER_RATE[k] * counts.get(k, 0) for k in WORKERS) * WAGE_MULT.get(wage, 1.0)


def ore_ev(kind: str, luck: float = 0.0) -> float:
    """作業員1人の、1時間あたりの鉱石の期待値（ペリカ）。"""
    return ORE_POWER[kind] * (1 + luck) * sum(p * v for _, _, _, p, v in ORES)


def roll_ores(kind: str, hours: float, luck: float, rng) -> dict:
    """作業員1人が hours 時間ぶん働いて掘った鉱石 {id: 個数}。端数の時間は、確率で1回にする。"""
    n = int(hours)
    if hours > n and rng.random() < hours - n:
        n += 1
    scale = ORE_POWER[kind] * (1 + luck)
    found: dict = {}
    for _ in range(n):
        x = rng.random()
        for oid, _, _, p, _ in ORES:  # 小さい確率の区間を順に重ねる
            x -= p * scale
            if x < 0:
                found[oid] = found.get(oid, 0) + 1
                break
    return found


def ore_gain(found: dict) -> int:
    return sum(ORE_VALUE[k] * n for k, n in found.items())


def idle_hours(since: float, now: float) -> float:
    """最後に受け取ってから、稼ぎに数える時間（STOCK_HOURS まで）。"""
    return max(0.0, min(STOCK_HOURS, (now - since) / 3600.0))


def settle_plan(
    workers: list, wage: str, renew: bool, idle_at: float, now: float, pot: int, balance: int, rng=None
) -> dict:
    """作業員の稼ぎと契約の決算（DB に触らない）。workers は [{id, kind, expires_at}]。

    稼ぎに数える時間は、最後に数えてから（STOCK_HOURS まで）。契約が切れた人は、
    - 自動更新オンで、契約料が払えれば、更新する（貯まった稼ぎから先に払い、足りなければ手元から）。新しい期限は、いまから CONTRACT_DAYS 日後
    - 払えない・オフなら、退職する（切れるまでの分だけ稼ぐ）
    低賃金なら、残った人から、確率で辞める人も出る。
    作業員は、働いた1時間ごとに鉱石を掘る（監督がいるほど、レア鉱石が出やすい）。売値は、gain に入る。
    {gain, ores: {鉱石: 個数}, pot_spent, balance_spent, renewed: [(id, 新しい期限)], removed: [id], quit: [id]} を返す。
    rng は、random() を持つもの（鉱石・辞める人の抽選。省略なら本物の乱数）。
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
        idle_rate(counts_of(active), wage) * hours
    )  # 契約が続く人の基本給が入った、貯まった稼ぎ（鉱石は、決算の前は数えない）
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
    gain = idle_rate(counts_of(staying), wage) * hours
    luck = foreman_luck(counts_of(staying)["foreman"])
    ores: dict = {}

    def dig(kind, h):
        for k, n in roll_ores(kind, h, luck, rng).items():
            ores[k] = ores.get(k, 0) + n

    for w in staying:
        dig(w["kind"], hours)
    for w in expired:  # 退職する人は、切れるまでの分だけ働く
        if w["id"] in removed:
            worked = max(0.0, min(now, w["expires_at"]) - start) / 3600.0
            gain += WORKER_RATE[w["kind"]] * WAGE_MULT.get(wage, 1.0) * worked
            dig(w["kind"], worked)
    quit_ids = []
    if wage == "low" and hours > 0:
        p = QUIT_PER_DAY * hours / 24.0
        quit_ids = [w["id"] for w in staying if rng.random() < p]
    return {
        "gain": int(gain) + ore_gain(ores),
        "ores": ores,
        "pot_spent": pot_spent,
        "balance_spent": balance_spent,
        "renewed": renewed,
        "removed": removed,
        "quit": quit_ids,
    }
