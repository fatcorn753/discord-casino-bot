"""帝愛ファイナンス・共済・貸付・CDS の行モデル。

対応する表の定義は infra/db/schema/tables.py。キーは `SELECT *` の列と同じ。
"""

from typing import TypedDict


class KyosaiAuctionRow(TypedDict):
    """帝愛の持ち口の競り（kyosai_auctions 表の行）。"""

    id: int
    guild_id: int
    units: int
    top_bid: int
    top_bidder: int | None
    ends: float
    status: str  # open / sold / unsold


class KyosaiOfferRow(TypedDict):
    """口の譲渡の申し出（kyosai_offers 表の行）。"""

    id: int
    guild_id: int
    seller: int
    buyer: int
    units: int
    price: int
    expires: float
    status: str  # open / done / cancelled
    done_at: float


class KyosaiMotionRow(TypedDict):
    """動議（kyosai_motions 表の行）。"""

    id: int
    guild_id: int
    proposer: int
    premium: float
    created: float
    closes: float
    status: str  # open / passed / rejected
    yes: int
    no: int
    channel_id: int
    message_id: int


class LoanRow(TypedDict):
    """貸し借り（loans 表の行）。"""

    id: int
    guild_id: int
    lender_id: int
    borrower_id: int
    principal: int
    rate_tenths: int
    term_days: int
    status: str  # offer / active / overdue / repaid / declined / cancelled / expired
    created: float
    offer_expires: float
    accepted_at: float | None
    due: float | None
    paid: int
    reminded: int
    channel_id: int
    message_id: int


class CdsRow(TypedDict):
    """CDS（破綻保証。cds 表の行）。"""

    id: int
    guild_id: int
    buyer_id: int
    target_id: int
    stake: int
    odds: float
    ts: float
    expires: float
    status: str  # active / paid / expired / void
    paid: int


class KyosaiAuctionEventRow(KyosaiAuctionRow):
    """競りで起きたこと（kyosai_auction_tick の返り値の要素）。"""

    event: str  # sold / unsold / listed


class KyosaiOfferDoneRow(KyosaiOfferRow):
    """譲渡の申し出を受けた結果（kyosai_accept の成功ペイロード）。"""

    fee: int


class KyosaiMotionProposeRow(TypedDict):
    """動議を出した結果（kyosai_propose の成功ペイロード）。"""

    id: int
    proposer: int
    premium: float
    closes: float


class KyosaiMotionViewRow(KyosaiMotionRow):
    """動議1件と流通口数（締め結果・ボタン書き換え用。kyosai_close_motions / kyosai_motion の返り値）。"""

    circulating: int


class LoanCollectRow(LoanRow):
    """期日の自動回収の結果（loan_collect_due の返り値の要素）。"""

    collected: int
    left: int


class BankruptResult(TypedDict):
    """自己破産の結果（bankrupt / bankrupt_for の成功ペイロード）。"""

    forced: bool  # 強制破産か
    cds_paid: int  # CDS の配当
    seized: int  # 差し押さえて換金した総額
    shop_value: int  # ショップの品の換金額
    senior_paid: int  # 帝愛（シニア債権）への返済
    junior_paid: int  # 貸付（ジュニア債権）への配当
    forgiven: int  # 免除された貸付の額
    compressed: int  # 圧縮された借金の額
    debt: int  # 破産後の借金
    seed: int  # 種銭として貸し付けた額


class EscapeResult(TypedDict):
    """海外逃亡の結果（escape_attempt の成功ペイロード）。"""

    escaped: bool
    fine: int  # 失敗時の罰金（借金に加わった額）
    debt: int  # 逃亡後の借金
    bankrupt: BankruptResult | None  # 成功したときの自己破産の結果


class KyosaiDaily(TypedDict):
    """共済の1日の処理の結果（kyosai_daily の返り値）。"""

    pool: int  # その日の配当の原資
    paid: int  # 配った合計
    recovered: int  # 休眠口座から回収した口数


class KyosaiStatus(TypedDict):
    """共済の状態の表示用まとめ（kyosai_status の返り値）。"""

    units: int  # 自分の持ち口
    total: int  # 流通する口数の合計
    holders: int  # 持ち主の数（帝愛を除く）
    house: int  # 帝愛の持ち口
    auction: KyosaiAuctionRow | None  # 開いている競り
    paid: float  # これまでに配った配当の累計
    charged: float  # これまでに請求した超過損失の累計
    next_dividend: float | None  # 次の配当の時刻
    pool: int  # いま配れる額
    theta: float | None  # 銀行の準備率
    locked: bool  # 共済の口の移動が封鎖中か
    premium: float  # 信託のプレミアム
    circulating: int  # 流通している口数
    motions: list[KyosaiMotionRow]  # 開いている動議（集計ずみ）
