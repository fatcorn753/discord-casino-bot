"""証券市場（現物・信用建玉・オプション・債券・市場イベント・決算・チャート）の行モデル。

対応する表の定義は infra/db/schema/tables.py。キーは `SELECT *` の列と同じ。
"""

from typing import TypedDict

from .core import HoldingBook, MarginPosition, MarginStatus, SpotHolding


class CryptoHoldingRow(TypedDict):
    """銘柄ごとの現物の保有（crypto 表の行）。"""

    guild_id: int
    user_id: int
    symbol: str
    units: int
    cost: int


class MarginRow(TypedDict):
    """信用の建玉（margin_positions 表の行プロジェクト）。"""

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
    final: float | None
    symbol2: str | None
    spot2: float | None
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
    checked: float | None
    rate: float | None


class MarketEventPlan(TypedDict):
    """新しい市場イベントの計画（id はまだ無い。event_step の force 引数・plan_event の返り値）。"""

    symbol: str
    announce_ts: float
    start_ts: float
    end_ts: float
    factor: float


class MarketEventRow(MarketEventPlan):
    """市場イベント（ボラティリティ予告。market_events 表の行）。"""

    id: int


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


class EarningsJump(TypedDict):
    """決算の発表による価格ジャンプ（オプションの値付け用。earnings_jumps の返り値）。"""

    symbol: str
    jump_at: float
    jump_var: float


class EarningsAnnouncement(TypedDict):
    """告知ずみの発表前の決算（earnings_upcoming の返り値。方向・大きさはまだ秘密）。"""

    id: int
    symbol: str
    announce_at: float
    release_at: float


class EarningsAnnounceRow(TypedDict):
    """告知すべき決算（earnings_to_announce の返り値）。"""

    id: int
    symbol: str
    release_at: float


class OptionBuyResult(TypedDict):
    """オプションを買った結果（option_buy の成功ペイロード）。"""

    id: int
    units: int
    cost: int
    strike: float
    expiry: float
    spot: float
    spot2: float | None


class OptionWriteResult(TypedDict):
    """裸売りした結果（option_write の成功ペイロード）。"""

    id: int
    units: int
    premium: int
    margin: int
    strike: float
    expiry: float
    spot: float


class OptionSettleRow(OptionRow):
    """決済したオプションの行（option_settle の返り値の要素）。"""

    gross: int


class InsiderStock(TypedDict):
    """裏情報メモの在庫（insider_stock の返り値）。"""

    left: int  # 残り
    total: int  # 全部の数


class HoldingsSnapshot(TypedDict):
    """保有のスナップショット（services の holdings_snapshot の返り値。満期の決済を済ませたあとの全保有）。"""

    prices: dict[str, float]  # {銘柄: いまの価格}
    spots: dict[str, SpotHolding]  # {銘柄: (口数,買った総額)}
    margins: dict[tuple[str, str], MarginPosition]  # {(向き,銘柄): (口数,建てた総額,担保)}
    options: HoldingBook[OptionRow]
    bond_data: HoldingBook[BondRow]
    option_quotes: dict[int, int]  # {オプションID: いまの買い戻し費用/売値}
    bond_quotes: dict[int, int]  # {債券ID: いま売ったときの手取り}
    margin_status: MarginStatus | None  # 建玉がなければ None
