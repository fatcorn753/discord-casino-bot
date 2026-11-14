"""帝愛ファイナンス（借金）と身分の数字。調整は catalogs/teiai.yaml を書き換える。

借金（users.debt）は、帝愛からの借金（シニア債権）。プレイヤー間の貸付（/貸付）は、そのあとに回収されるジュニア債権。

- 借金に落ちたとき、現金 → 空売り → CSトークン → 通常債の順に強制執行（差し押さえ）してから、残りを借金にする
- 借金がある人の手元が空なら、帝愛が種銭 SEED を貸し付ける（借金に加算）。デイリーでも、手元が SEED 未満なら補充する
- 入ってきた収入（ゲームの純利益・デイリー・ミッション報酬など）の REPAY_RATE を、借金の返済へ天引きする。残りは手元に残る
- デイリーは、先に SKIM_RATE を帝愛が没収する（借金の元本は減らない）
- 身分: カタギ（借金・延滞なし）／地下労働者（借金か延滞がある）／破産者（自己破産のあと、完済して BLACKLIST_DAYS 日たつまで）
- 自己破産: 全財産とショップの全品を差し押さえ、換金して債権者へ配当する。貸付の残りは免責、帝愛の借金は BANKRUPT_FLOOR まで圧縮
"""

from typing import Optional

from infra import catalogs

_CFG = catalogs.teiai()

SEED = _CFG.seed  # 帝愛が貸し付ける種銭の枠（手元がこの額になるまで補充する）
LABOR_SKIM_RATE = _CFG.labor_skim_rate  # 地下作業の稼ぎを受け取るときに、ピンハネされる割合
HORSE_PRIZE_SKIM_RATE = _CFG.horse_prize_skim_rate  # 競馬の賞金が出たときに、ピンハネされる割合（買った馬は撤廃）
HORSE_RENTAL_EXTRA_SKIM_RATE = _CFG.horse_rental_extra_skim_rate  # レンタル馬の賞金に加わる割合
SKIM_RATE = _CFG.skim_rate  # 借金中のデイリーから、帝愛が没収する割合
REPAY_RATE = _CFG.repay_rate  # 収入から、借金の返済へ天引きする割合
BANKRUPT_FLOOR = _CFG.bankrupt_floor  # 自己破産のあとに残る、帝愛の借金の上限
BLACKLIST_DAYS = _CFG.blacklist_days  # 破産者の身分が続く、完済後の日数
ESCAPE_CHANCE = _CFG.escape_chance  # 海外逃亡の成功率
ESCAPE_MIN_DEBT = _CFG.escape_min_debt  # 逃亡を試せる、帝愛の借金の下限
ESCAPE_FINE_RATE = _CFG.escape_fine_rate  # 失敗したときの罰金（借金に対する割合）
ESCAPE_FINE_MIN = _CFG.escape_fine_min  # 罰金の最低額
ESCAPE_MAX_TRIES = _CFG.escape_max_tries  # 海外逃亡を試せる回数
ESCAPE_COOLDOWN_HOURS = _CFG.escape_cooldown_hours  # 失敗のあと、もう一度試せるまでの時間
AUCTION_START = _CFG.auction_start  # 差し押さえた品を競売に出すときの、最初の値段（定価に対して）
AUCTION_STEP = _CFG.auction_step  # 1日ごとの値下がり（定価に対して）
AUCTION_FLOOR = _CFG.auction_floor  # 値下がりの下限
AUCTION_YIELD_SHARE = _CFG.auction_yield_share  # 競売の売上のうち、預金者の利回りへ回す割合
SEIZE_RATE = _CFG.seize_rate  # ショップの品と、解約できない債券を差し押さえるときの換金率

# ---- 帝愛の利息（トイチ）----
DEBT_RATE_PER_DAY = _CFG.debt_rate_per_day  # 借金にかかる日利（10日で1割＝トイチ）
DEBT_INTEREST_MAX_DAYS = _CFG.debt_interest_max_days  # 1回の決算で数える日数の上限
# ---- 破産猶予（取り立てのカウントダウン）----
GRACE_DEBT = _CFG.grace_debt  # 帝愛への借金がこの額以上になると、破産猶予に入る
DEBT_INTEREST_CEILING = GRACE_DEBT - 1  # 利息で借金が増えるのは、ここまで
GRACE_HOURS = _CFG.grace_hours  # 猶予の長さ

# ---- 帝愛預金（部分準備銀行）----
BANK_MIN_DEPOSIT = _CFG.bank_min_deposit
RESERVE_RATIO = _CFG.reserve_ratio
LOAN_RECOVERY_PER_HOUR = _CFG.loan_recovery_per_hour
LOCK_DEBT_RATE = _CFG.lock_debt_rate
LOSS_SHARE = _CFG.loss_share
PREMIUM_YIELD_SHARE = _CFG.premium_yield_share
TAX_YIELD_SHARE = _CFG.tax_yield_share
SKIM_YIELD_SHARE = _CFG.skim_yield_share
RUN_WARN_RATIO = _CFG.run_warn_ratio
RUN_DANGER_RATIO = _CFG.run_danger_ratio

# ---- CDS（破綻保証）----
CDS_MIN_STAKE = _CFG.cds_min_stake
CDS_MIN_ODDS, CDS_MAX_ODDS = _CFG.cds_min_odds, _CFG.cds_max_odds
CDS_TERM_DAYS = _CFG.cds_term_days
CDS_SELF_CAP = _CFG.cds_self_cap
CDS_PAYOUT_CAP_RATE = _CFG.cds_payout_cap_rate

KATAGI, UNDERGROUND, BANKRUPT = "katagi", "underground", "bankrupt"
WEALTHY = "wealthy"
WEALTHY_ASSETS = _CFG.wealthy_assets
STATUS_LABEL = {k: v.label for k, v in _CFG.statuses.items()}
STATUS_MARK = {k: v.mark for k, v in _CFG.statuses.items() if v.mark is not None}
STATUS_NAME = {k: v.name for k, v in _CFG.statuses.items() if v.name is not None}


def withhold(income: int) -> int:
    """収入から天引きする額。"""
    return int(income * REPAY_RATE)


def skim(amount: int) -> int:
    """デイリーから没収する額。"""
    return int(amount * SKIM_RATE)


def labor_skim(amount: int) -> int:
    """地下作業の稼ぎから、帝愛がピンハネする額。"""
    return int(amount * LABOR_SKIM_RATE)


def cds_odds(status: str, debt: int, margin_ratio: Optional[float] = None) -> float:
    """CDS のオッズ（倍率）。"""
    r = 0.04 if status == KATAGI else 0.25 + 0.5 * min(1.0, debt / GRACE_DEBT)
    if margin_ratio is not None and margin_ratio < 1.5:
        r += 0.1
    return round(min(CDS_MAX_ODDS, max(CDS_MIN_ODDS, 0.85 / r)), 1)


def auction_rate(listed_at: float, now: float) -> float:
    """競売に出してから時間がたった分だけ値下がりした、いまの値段（定価に対する割合）。下限は AUCTION_FLOOR。"""
    days = max(0, int((now - listed_at) // 86400))
    return max(AUCTION_FLOOR, AUCTION_START - AUCTION_STEP * days)
