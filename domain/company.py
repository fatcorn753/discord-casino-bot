"""会社（自分だけの会社・事業所）の数字。調整は catalogs/company.yaml。Discord と DB に依存しない純粋なロジック。

- 会社は1人1社。事業所は1社に1つで、Lv1〜5。建設費の累計（invested）が、生産と維持費の元になる。
- 事業所は時間で製品を作り、倉庫（STORAGE_DAYS 日分）に貯める。あふれた分は捨てる。
- 製品は、出荷した日の相場で帝愛が買い取る。相場と、その日の生産量は、日替わりの状態（condition）で決まる。
  状態は、種類・サーバー・日付から決まる（同じ日なら、誰が見ても同じ）。
- 在庫（stock）は「買取価格 1.0 倍のときの額」で持つ。出荷額 = 在庫 × その日の価格。
"""

import datetime
import random
from dataclasses import dataclass

from domain.errors import Outcome, fail
from domain.ports import get_catalogs
from domain.ports.catalog_types.company import CompanyConditionContract, CompanyKindContract

_CFG = get_catalogs().company()

FOUND_FEE = _CFG.found_fee
NAME_MAX = _CFG.name_max
BUILD_COSTS = tuple(_CFG.build_costs)
MAX_LEVEL = len(BUILD_COSTS)
UPKEEP_RATE = _CFG.upkeep_rate
PINHANE_RATE = _CFG.pinhane_rate
STORAGE_DAYS = _CFG.storage_days
BANKRUPT_DAYS = _CFG.bankrupt_days
DAILY_GRANT = _CFG.daily_grant  # 胴元の口座が足りないとき、帝愛が新しく出す買取代金の、1日の上限
STAKE_MIN = _CFG.stake_min  # 1回の出資の最低額
STAKE_CAP_RATIO = _CFG.stake_cap_ratio  # 1社への出資の合計の上限（建設費の累計に対して）
DIVIDEND_MAX = _CFG.dividend_max  # 配当率の上限
KINDS: dict[str, CompanyKindContract] = {k.key: k for k in _CFG.kinds}
KIND_NAMES = {k.key: k.name for k in _CFG.kinds}

BANNED_NAME_CHARS = "@<>`*_~|#:/\\\n\r"
DAY_SECONDS = 86400


@dataclass(frozen=True)
class DayState:
    """ある日の、ある種類の事業所の状態。"""

    condition: CompanyConditionContract
    price: float  # その日の買取価格（倍率）

    @property
    def yield_mult(self) -> float:
        return self.condition.yield_mult


def check_name(name: str) -> Outcome[str]:
    """社名が使えるか。(使えるか, 整えた社名か Failure)。メンション・リンク・装飾に使う文字は禁止。"""
    name = name.strip()
    if not name:
        return False, fail("company_name_empty")
    if len(name) > NAME_MAX:
        return False, fail("company_name_too_long", max=NAME_MAX)
    if any(c in BANNED_NAME_CHARS for c in name) or "http" in name.lower():
        return False, fail("company_name_invalid_chars")
    return True, name


def invested(level: int) -> int:
    """事業所を level にするまでに払った建設費の累計。"""
    return sum(BUILD_COSTS[:level])


def upgrade_cost(level: int) -> int | None:
    """いまの level から、次のレベルにする建設費。最大なら None。"""
    return BUILD_COSTS[level] if level < MAX_LEVEL else None


def daily_output(kind: str, level: int) -> float:
    """ふつうの日（生産 1.0 倍）の、1日の生産（買取価格 1.0 倍のときの額）。"""
    return invested(level) * KINDS[kind].yield_rate


def storage_cap(kind: str, level: int) -> float:
    return daily_output(kind, level) * STORAGE_DAYS


def daily_upkeep(level: int) -> int:
    return int(invested(level) * UPKEEP_RATE)


def day_of(ts: float, tz_hours: float) -> str:
    """ts の、現地時間（UTC から tz_hours 時間ずれた時刻）での日付。tz_hours は IClock.offset_hours() の値を渡す。"""
    tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
    return datetime.datetime.fromtimestamp(ts, tz).date().isoformat()


def day_start(day: str, tz_hours: float) -> float:
    tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
    return datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(), tzinfo=tz).timestamp()


def next_day(day: str) -> str:
    return (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()


def day_state(kind: str, guild_id: int, day: str) -> DayState:
    """その日の状態と買取価格。種類・サーバー・日付で決まる。"""
    rng = random.Random(f"company:{kind}:{guild_id}:{day}")
    conditions = KINDS[kind].conditions
    cond = rng.choices(conditions, weights=[c.weight for c in conditions], k=1)[0]
    return DayState(cond, round(rng.uniform(cond.price_lo, cond.price_hi), 2))


def produce(kind: str, level: int, guild_id: int, stock: float, since: float, now: float, tz_hours: float) -> float:
    """since から now までの生産を足した在庫。日をまたぐと、日ごとの生産量で積む。倉庫からあふれた分は捨てる。"""
    cap = storage_cap(kind, level)
    if now <= since:
        return min(stock, cap)
    per_second = daily_output(kind, level) / DAY_SECONDS
    cursor, day = since, day_of(since, tz_hours)
    while cursor < now and stock < cap:
        end = min(now, day_start(next_day(day), tz_hours))
        stock += (end - cursor) * per_second * day_state(kind, guild_id, day).yield_mult
        cursor, day = end, next_day(day)
    return min(stock, cap)


def sale(stock: float, price: float, budget: int) -> tuple[int, int, float]:
    """在庫を price で出荷する。(会社に入る額, ピンハネ, 残る在庫)。

    帝愛の買取予算 budget（胴元の口座の残高と、今日の買取枠の残り）を超えては払わない。足りないときは、払える分だけを出荷して、残りは在庫に残す。
    """
    gross = int(stock * price)
    net = gross - int(gross * PINHANE_RATE)
    if net <= 0:
        return 0, 0, stock
    if net <= budget:
        return net, gross - net, 0.0
    fraction = budget / net
    sold_gross = int(gross * fraction)
    paid = min(budget, sold_gross - int(sold_gross * PINHANE_RATE))
    return paid, sold_gross - paid, stock * (1 - fraction)


# ---------------------------------------------------------------------- #
# ほかの人の会社への出資（#71）
# ---------------------------------------------------------------------- #
def stake_cap(level: int) -> int:
    """1社が受けられる出資の合計の上限。"""
    return int(invested(level) * STAKE_CAP_RATIO)


def dividend_split(paid: int, rate: float, stakes: dict[int, int]) -> dict[int, int]:
    """出荷の買取代金 paid のうち rate を、出資額の比で出資者に配る。端数は切り捨て（会社に残る）。"""
    total = sum(stakes.values())
    pool = int(paid * max(0.0, min(DIVIDEND_MAX, rate)))
    if pool <= 0 or total <= 0:
        return {}
    out = {uid: pool * amt // total for uid, amt in stakes.items()}
    return {uid: v for uid, v in out.items() if v > 0}


def stake_values(company_value: int, stakes: dict[int, int]) -> tuple[int, dict[int, int]]:
    """会社の価値（会社資金 + 建設費の累計）を、社長と出資者に分ける。

    出資者は出資額で数える。ただし会社の価値が出資の合計を下回ったら、出資額の比で目減りさせる。
    社長は残り（0 未満にはしない）。同じお金を二重に数えないため。
    """
    total = sum(stakes.values())
    if total <= 0:
        return max(0, company_value), {}
    scale = min(1.0, max(0, company_value) / total)
    investors = {uid: int(amt * scale) for uid, amt in stakes.items()}
    return max(0, company_value - total), investors
