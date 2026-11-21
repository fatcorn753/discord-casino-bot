"""帝愛ファイナンス・共済・貸付・CDS の行モデル。

対応する表の定義は infra/db/schema/tables.py。キーは `SELECT *` の列と同じ。
"""

from typing import Optional, TypedDict


class KyosaiAuctionRow(TypedDict):
    """帝愛の持ち口の競り（kyosai_auctions 表の行）。"""

    id: int
    guild_id: int
    units: int
    top_bid: int
    top_bidder: Optional[int]
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
    accepted_at: Optional[float]
    due: Optional[float]
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
