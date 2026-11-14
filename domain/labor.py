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

from domain.ports import get_catalogs

_CFG = get_catalogs().labor()

TOOLS = tuple(t.key for t in _CFG.tools)
TOOL_NAME = {t.key: t.name for t in _CFG.tools}
TOOL_MARK = {t.key: t.mark for t in _CFG.tools}
TOOL_EFFECT = {t.key: t.effect for t in _CFG.tools}
MAX_LEVEL = _CFG.max_level
UPGRADE_COST = _CFG.upgrade_cost

MAX_CLICKS_PER_DAY = _CFG.max_clicks_per_day
OVER_CAP_PAY = _CFG.over_cap_pay

BASE_PAY = _CFG.base_pay
PAY_PER_LEVEL = _CFG.pay_per_level
FATIGUE_MAX = _CFG.fatigue_max
FATIGUE_GAIN = _CFG.fatigue_gain
GAIN_CUT_PER_LEVEL = _CFG.gain_cut_per_level
LATE_GAIN_CUT_PER_LEVEL = _CFG.late_gain_cut_per_level
EARLY_LEVELS = _CFG.early_levels
MIN_PAY_RATE = _CFG.min_pay_rate
RECOVER_PER_SEC = _CFG.recover_per_sec

DRINK_RECOVER = _CFG.drink_recover
CLICK_INTERVAL = _CFG.click_interval


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
CRIT_BASE = _CFG.crit_base
CRIT_PER_LEVEL = _CFG.crit_per_level
CRIT_WORKER_BONUS = _CFG.crit_worker_bonus
CRIT_MAX = _CFG.crit_max
CRIT_MULT = _CFG.crit_mult
CRIT_ORE_CHANCE = _CFG.crit_ore_chance
CRIT_ORES = tuple((o.id, o.name, o.mark, o.weight, o.value) for o in _CFG.crit_ores)

COMBO_SIZE = _CFG.combo_size
COMBO_BONUS = _CFG.combo_bonus

TERRAIN_KINDS = _CFG.terrain_kinds
TERRAIN_HIT_BONUS = _CFG.terrain_hit_bonus
TERRAIN_FATIGUE_CUT = _CFG.terrain_fatigue_cut
TERRAIN_SILVER_MULT = _CFG.terrain_silver_mult


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
WORKERS = tuple(w.key for w in _CFG.workers)
WORKER_NAME = {w.key: w.name for w in _CFG.workers}
WORKER_MARK = {w.key: w.mark for w in _CFG.workers}
WORKER_RATE = {w.key: w.rate for w in _CFG.workers}
CONTRACT_DAYS = _CFG.contract_days
CONTRACT_FEE = {w.key: w.contract_fee for w in _CFG.workers}
FOREMAN_LUCK = _CFG.foreman_luck

COAL_P = _CFG.coal_p

ORES = tuple((o.id, o.name, o.mark, COAL_P if o.prob is None else o.prob, o.value) for o in _CFG.ores)
FLAT = _CFG.flat_ores
ORE_NAME = {o[0]: o[1] for o in ORES}
ORE_MARK = {o[0]: o[2] for o in ORES}
ORE_VALUE = {o[0]: o[4] for o in ORES}
ORE_BIG = _CFG.ore_big
ORE_POWER = {w.key: w.ore_power for w in _CFG.workers}

RELICS = tuple((r.id, r.name, r.mark, r.prob, r.value) for r in _CFG.relics)
RELIC_ID = {r[0]: f"relic_{r[0]}" for r in RELICS}
RELIC_BIG = _CFG.relic_big
RELIC_POWER = {w.key: w.relic_power for w in _CFG.workers}

SLOTS_BASE = _CFG.slots_base
DORM_COST = _CFG.dorm_cost
DORM_MAX = _CFG.dorm_max

STOCK_HOURS = _CFG.stock_hours


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
CLICK_GAP_MAX = _CFG.click_gap_max
MECH_CV_MAX = _CFG.mech_cv_max
MECH_MIN_CLICKS = _CFG.mech_min_clicks
SUSPECT_HOURS = _CFG.suspect_hours


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
