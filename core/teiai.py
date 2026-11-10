"""帝愛ファイナンス（借金）と身分の数字。調整はここの定数を書き換える。

借金（users.debt）は、帝愛からの借金（シニア債権）。プレイヤー間の貸付（/貸付）は、そのあとに回収されるジュニア債権。

- 借金に落ちたとき、現金 → 空売り → CSトークン → 通常債の順に強制執行（差し押さえ）してから、残りを借金にする
- 借金がある人の手元が空なら、帝愛が種銭 SEED を貸し付ける（借金に加算）。デイリーでも、手元が SEED 未満なら補充する
- 入ってきた収入（ゲームの純利益・デイリー・ミッション報酬など）の REPAY_RATE を、借金の返済へ天引きする。残りは手元に残る
- デイリーは、先に SKIM_RATE を帝愛が没収する（借金の元本は減らない）
- 身分: カタギ（借金・延滞なし）／地下労働者（借金か延滞がある）／破産者（自己破産のあと、完済して BLACKLIST_DAYS 日たつまで）
- 自己破産: 全財産とショップの全品を差し押さえ、換金して債権者へ配当する。貸付の残りは免責、帝愛の借金は BANKRUPT_FLOOR まで圧縮
"""

from typing import Optional

SEED = 3_000  # 帝愛が貸し付ける種銭の枠（手元がこの額になるまで補充する）
LABOR_SKIM_RATE = 0.01  # 地下作業の稼ぎを受け取るときに、ピンハネされる割合（借金の有無に関わらず、全員。残りが手元に入る）。画面には「慈善団体への寄付」と出す
SKIM_RATE = 0.20  # 借金中のデイリーから、帝愛が没収する割合
REPAY_RATE = 0.30  # 収入から、借金の返済へ天引きする割合
BANKRUPT_FLOOR = 10_000  # 自己破産のあとに残る、帝愛の借金の上限（これより多い分は圧縮）
BLACKLIST_DAYS = 7  # 破産者の身分が続く、完済後の日数
ESCAPE_CHANCE = 0.07  # 海外逃亡の成功率。成功すると、全財産を置いていく代わりに、帝愛の借金が全額ゼロになる
ESCAPE_MIN_DEBT = 5_000  # 逃亡を試せる、帝愛の借金の下限（種銭だけの人が、気軽に押せないように）
ESCAPE_FINE_RATE = 0.10  # 失敗したときの罰金（借金に加わる。借金に対する割合）
ESCAPE_FINE_MIN = 1_000  # 罰金の最低額
ESCAPE_COOLDOWN_HOURS = 24  # 失敗のあと、もう一度試せるまでの時間
AUCTION_START = 0.90  # 差し押さえた品を競売に出すときの、最初の値段（定価に対して）
AUCTION_STEP = 0.10  # 1日ごとの値下がり（定価に対して）
AUCTION_FLOOR = 0.70  # 値下がりの下限。SEIZE_RATE より高くして、買い直して破産し直すと損をするようにする。ここで止まるので、売れ残る品があってもよい
AUCTION_YIELD_SHARE = 0.5  # 競売の売上のうち、預金者の利回りへ回す割合
SEIZE_RATE = 0.50  # ショップの品と、解約できない債券を差し押さえるときの換金率（購入額に対して）

# ---- 帝愛の利息（トイチ）----
DEBT_RATE_PER_DAY = 0.01  # 借金にかかる日利（10日で1割＝トイチ）。経過した日数ぶんの単利を、借金に加える
DEBT_INTEREST_MAX_DAYS = (
    10  # 1回の決算で数える日数の上限。しばらく来なかった人の借金が、一気に膨らまないようにする（放置死の防止）
)
# ---- 破産猶予（取り立てのカウントダウン）----
GRACE_DEBT = 15_000  # 帝愛への借金がこの額以上になると、破産猶予に入る
DEBT_INTEREST_CEILING = GRACE_DEBT - 1  # 利息で借金が増えるのは、ここまで
GRACE_HOURS = 24  # 猶予の長さ。この間に借金を GRACE_DEBT 未満へ戻せば回避、時間切れで自動の強制破産

# ---- 帝愛預金（部分準備銀行）----
BANK_MIN_DEPOSIT = 100
RESERVE_RATIO = 0.20  # 預けられた額のうち、すぐ払い出せる現金として手元に置く割合（残りは「地下融資」に回す）
LOAN_RECOVERY_PER_HOUR = 0.02  # 地下融資が、1時間あたりこの割合ずつ手元の現金に戻ってくる
LOCK_DEBT_RATE = 0.05  # 地下労働者の借金の、この割合を、貸し倒れに備えて現金からロックする
LOSS_SHARE = 0.03  # カジノでプレイヤーが負けた額の、この割合を、預金者の利回りへ回す
PREMIUM_YIELD_SHARE = 0.5  # CDS の掛け金のうち、預金者の利回りへ回す割合（残りは帝愛の手元現金に残る）
TAX_YIELD_SHARE = 1.0  # 株の利益への税（キャピタルゲイン税）のうち、預金者の利回りへ回す割合（残りは銀行の手元）
SKIM_YIELD_SHARE = 0.5  # デイリーのピンハネのうち、預金者の利回りへ回す割合
RUN_WARN_RATIO = 0.15  # 利用可能現金 ÷ 預金総額 がこれ未満なら 🟡、RUN_DANGER_RATIO 未満なら 🔴
RUN_DANGER_RATIO = 0.05

# ---- CDS（破綻保証）----
CDS_MIN_STAKE = 100
CDS_MIN_ODDS, CDS_MAX_ODDS = 1.2, 15.0
CDS_TERM_DAYS = 14  # この間に、対象が強制破産しなければ、掛け金は戻らず失効
CDS_SELF_CAP = 50_000  # 自分への CDS の、配当の見込み（掛け金 × オッズ）の合計の上限（保証を無制限に積めない）
CDS_PAYOUT_CAP_RATE = 0.50  # 他人への CDS の配当の合計は、対象の破産時の借金のこの割合まで（結託しても損にする）

KATAGI, UNDERGROUND, BANKRUPT = "katagi", "underground", "bankrupt"
STATUS_LABEL = {KATAGI: "🟢 カタギ", UNDERGROUND: "🟡 地下労働者", BANKRUPT: "🔴 破産者"}
STATUS_NAME = {UNDERGROUND: "地下労働者", BANKRUPT: "破産者"}  # ニックネームの末尾に【…】で付ける名前


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
    """CDS のオッズ（倍率）。対象の危険度 r に連動し、オッズ = 0.85 ÷ r（MIN〜MAX に収める）。

    r: カタギ 0.04 / 地下労働者 0.25〜0.75（借金が GRACE_DEBT に近いほど高い）。信用取引の維持率が 150% 未満なら +0.1。
    危なくなるほど安く買えなくなる（買い遅れると、配当が小さい）。
    """
    r = 0.04 if status == KATAGI else 0.25 + 0.5 * min(1.0, debt / GRACE_DEBT)
    if margin_ratio is not None and margin_ratio < 1.5:
        r += 0.1
    return round(min(CDS_MAX_ODDS, max(CDS_MIN_ODDS, 0.85 / r)), 1)


def auction_rate(listed_at: float, now: float) -> float:
    """競売に出してから時間がたった分だけ値下がりした、いまの値段（定価に対する割合）。下限は AUCTION_FLOOR。"""
    days = max(0, int((now - listed_at) // 86400))
    return max(AUCTION_FLOOR, AUCTION_START - AUCTION_STEP * days)
