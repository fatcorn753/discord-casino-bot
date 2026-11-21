"""証券市場（現物・信用建玉・オプション・債券・市場イベント・決算・チャート）の行モデル。

対応する表の定義は infra/db/schema/tables.py。キーは `SELECT *` の列と同じ。
"""

from typing import Optional, TypedDict


class CryptoHoldingRow(TypedDict):
    """銘柄ごとの現物の保有（crypto 表の行）。"""

    guild_id: int
    user_id: int
    symbol: str
    units: int
    cost: int


class MarginRow(TypedDict):
    """信用の建玉（crypto_short / crypto_long 表の行。空売りと信用買いで同じ形）。"""

    guild_id: int
    user_id: int
    symbol: str
    units: int
    entry_value: float
    margin: int
    interest_at: float
    accrued: float


class OptionRow(TypedDict):
    """オプション（options 表の行）。"""

    id: int
    guild_id: int
    user_id: int
    kind: str  # call / put
    symbol: str
    strike: float
    units: int
    premium: int
    expiry: float
    spot: float
    ts: float
    status: str  # active / exercised / expired / sold
    paid: int
    final: Optional[float]
    symbol2: Optional[str]
    spot2: Optional[float]
    side: str  # long（買い）/ short（裸売り）
    margin: int


class BondRow(TypedDict):
    """債券（bonds 表の行）。"""

    id: int
    guild_id: int
    user_id: int
    kind: str  # normal / junk
    principal: int
    due: float
    status: str  # active / paid / defaulted / cancelled
    paid: int
    ts: float
    checked: Optional[float]
    rate: Optional[float]


class MarketEventRow(TypedDict):
    """市場イベント（ボラティリティ予告。market_events 表の行）。"""

    id: int
    symbol: str
    announce_ts: float
    start_ts: float
    end_ts: float
    factor: float


class EarningsRow(TypedDict):
    """定期決算（earnings 表の行）。"""

    id: int
    symbol: str
    announce_at: float
    release_at: float
    direction: int  # 1 好決算 / -1 悪決算
    size: float
    announced: int
    released: int


class ChartPostRow(TypedDict):
    """市況チャートの投稿（chart_posts 表の行）。"""

    channel_id: int
    message_id: int
    guild_id: int
    period: str
    ts: float
    live: int
    symbol: str


class ChartBoardRow(TypedDict):
    """市況掲示板の投稿（chart_boards 表の行）。"""

    guild_id: int
    channel_id: int
    message_id: int
