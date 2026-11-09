"""地下作業（クリッカー）の数字と計算。DB にも Discord にも触らない（処理は core/eco_labor.py、画面は cogs/labor_cog.py）。

ペリカが尽きた人の救済。元手は 0。「働く」ボタンを押すとペリカが貯まる。
- 疲労: 押すたびに溜まり、溜まるほど1回の稼ぎが減る。時間で自然に回復する（事故も、休憩もない）
- 工具: ツルハシ（稼ぎ）・手袋（疲労の溜まりにくさ）を、稼いだペリカで強化する
1日の上限や、手元の制限はない（連打は、手間がかかるので、続かない）。稼ぎの速さは、疲労で自然に頭打ちになる。
作業員は、7日の契約で雇う（自動更新）。契約料が、ペリカの使い道になる。
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
GAIN_CUT_PER_LEVEL = 0.4  # 手袋1段ごとの軽減（最大で 1.0）
MIN_PAY_RATE = 0.4  # 疲労が満タンのときの、稼ぎの割合（空のときは 1.0）
RECOVER_PER_SEC = 0.2  # 疲労の自然回復（100 回復するのに約8分）

DRINK_RECOVER = 60.0  # 栄養ドリンク（ショップ）1本で回復する疲労
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
WORKER_RATE = {"apprentice": 10, "miner": 35, "craftsman": 80, "foreman": 0}  # 1時間の稼ぎ（監督は自分では稼がない）
CONTRACT_DAYS = 7  # 契約の期間（日）。切れたら、自動更新（オンの場合）か、退職
# 契約料（雇うときと、更新のときに払う）。1日1回受け取る人が、契約の1週間（84時間分）に稼ぐ額の、4割ほど
CONTRACT_FEE = {"apprentice": 340, "miner": 1_200, "craftsman": 2_700, "foreman": 1_500}
FOREMAN_BONUS = 0.08  # 監督1人ごとに、全員の稼ぎが増える割合
FOREMAN_BONUS_CAP = 0.40  # 監督の加算の上限（5人分）

SLOTS_BASE = 5  # 雇える人数（監督も数える）
DORM_COST = (2_000, 6_000, 15_000, 40_000, 100_000)  # 寮を1段上げる値段。1段ごとに +1人
DORM_MAX = 5

STOCK_HOURS = 12.0  # 受け取らずに貯められる時間（超えた分は消える）
WAGES = ("normal", "low")
WAGE_NAME = {"normal": "標準", "low": "低賃金"}
WAGE_MULT = {"normal": 1.0, "low": 1.25}  # 低賃金は、取り分が増える代わりに、作業員が辞める
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


def foreman_bonus(foremen: int) -> float:
    return min(FOREMAN_BONUS_CAP, FOREMAN_BONUS * max(0, foremen))


def idle_rate(counts: dict, wage: str) -> float:
    """作業員たちの、1時間あたりの稼ぎ（ペリカ）。counts は {種類: 人数}。"""
    base = sum(WORKER_RATE[k] * counts.get(k, 0) for k in WORKERS)
    return base * (1 + foreman_bonus(counts.get("foreman", 0))) * WAGE_MULT.get(wage, 1.0)


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
    {gain, pot_spent, balance_spent, renewed: [(id, 新しい期限)], removed: [id], quit: [id]} を返す。
    """
    hours = idle_hours(idle_at, now) if idle_at > 0 else 0.0
    start = now - hours * 3600
    active = [w for w in workers if w["expires_at"] > now]
    expired = sorted((w for w in workers if w["expires_at"] <= now), key=lambda w: -CONTRACT_FEE[w["kind"]])

    def counts_of(ws):
        c = {k: 0 for k in WORKERS}
        for w in ws:
            c[w["kind"]] += 1
        return c

    avail_pot = pot + int(idle_rate(counts_of(active), wage) * hours)  # 契約が続く人の稼ぎが入った、貯まった稼ぎ
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
    for w in expired:  # 退職する人は、切れるまでの分だけ稼ぐ（監督の上乗せは付かない）
        if w["id"] in removed:
            worked = max(0.0, min(now, w["expires_at"]) - start) / 3600.0
            gain += WORKER_RATE[w["kind"]] * WAGE_MULT.get(wage, 1.0) * worked
    quit_ids = []
    if wage == "low" and hours > 0:
        rng = rng or random.SystemRandom()
        p = QUIT_PER_DAY * hours / 24.0
        quit_ids = [w["id"] for w in staying if rng.random() < p]
    return {
        "gain": int(gain),
        "pot_spent": pot_spent,
        "balance_spent": balance_spent,
        "renewed": renewed,
        "removed": removed,
        "quit": quit_ids,
    }
