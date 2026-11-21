"""ソーシャル機能（P2P出品・抽選・投票・クイズ・地下作業・ショップ・会社）の行モデル。

対応する表の定義は infra/db/schema/tables.py。キーは `SELECT *` の列と同じ。
"""

from typing import Optional, TypedDict


class ListingRow(TypedDict):
    """プレイヤー間の市場の出品（market_listings 表の行）。"""

    id: int
    guild_id: int
    seller_id: int
    kind: str  # item / horse
    ref: str  # 品のID、または馬の番号
    qty: int
    mode: str  # fixed（定価）/ auction（競り）
    price: int
    bid: int
    bidder_id: int
    created: float
    end_at: float
    status: str  # active / sold / cancelled / expired
    buyer_id: int
    sold_price: int
    closed_at: Optional[float]
    channel_id: int
    message_id: int
    post_done: int


class RaffleRow(TypedDict):
    """抽選（raffle 表の行）。"""

    id: int
    guild_id: int
    host_id: int
    fee: int
    closes: float
    channel_id: int
    message_id: int
    status: str  # open / done / cancelled


class PollRow(TypedDict):
    """投票（polls 表の行）。"""

    id: int
    guild_id: int
    creator: int
    title: str
    closes: float
    channel_id: int
    message_id: int
    adj_yes: int
    adj_no: int
    rigs: int
    status: str  # open / closed


class QuizRow(TypedDict):
    """クイズの問題（quiz_questions 表の行）。"""

    id: int
    question: str
    choices: str  # JSON の配列。先頭が正解
    explanation: str
    shown_order: str  # 表示する並び（choices の添字を並べ替えた JSON）
    author_id: int
    guild_id: int
    channel_id: int
    message_id: int
    settled: int
    earned: int
    creator_name: str
    msg_right: str
    msg_wrong: str
    reward: int
    expires: Optional[float]
    created: float


class LaborRow(TypedDict):
    """地下作業の状態（labor 表の行）。"""

    guild_id: int
    user_id: int
    pick: int
    glove: int
    helmet: int
    fatigue: float
    fatigue_at: float
    pot: int
    pot_workers: int
    donated: int
    relics: str  # JSON {品のID: 個数}
    day: str
    day_earned: int
    day_click: int
    last_click: float
    rest_until: float
    clicks: int
    earned_total: int
    apprentice: int
    miner: int
    craftsman: int
    foreman: int
    dorm: int
    idle_at: float
    renew: int
    ores: str  # JSON {鉱石: 個数}


class LaborFindRow(TypedDict):
    """作業員の掘り出しの履歴（labor_finds 表の行）。"""

    id: int
    guild_id: int
    user_id: int
    at: float
    kind: str
    ore: str
    qty: int
    collected: int


class LaborClickHourRow(TypedDict):
    """「働く」を押した記録の1時間ごとの集計（labor_click_hours 表の行）。"""

    guild_id: int
    user_id: int
    hour: int
    clicks: int
    pay: int
    gaps: int
    sum_gap: float
    sum_gap2: float
    min_gap: Optional[float]


class LaborWorkerRow(TypedDict):
    """雇っている作業員（labor_workers 表の行）。"""

    id: int
    guild_id: int
    user_id: int
    kind: str
    expires_at: float
    legacy: int


class ShopStockRow(TypedDict):
    """ショップの在庫（shop_stock 表の行）。"""

    guild_id: int
    user_id: int
    item_id: str
    stock: float
    updated: float


class ProfileRow(TypedDict):
    """プロフィール（profile 表の行）。"""

    guild_id: int
    user_id: int
    title: Optional[str]
    card_color: Optional[str]
    card_icon: Optional[str]
    skin_dice: Optional[str]
    skin_table: Optional[str]
    daily_boost: int
    insurance: int


class CompanyRow(TypedDict):
    """会社（companies 表の行）。"""

    guild_id: int
    user_id: int
    name: str
    kind: str
    level: int
    funds: int
    stock: float
    stock_ts: float
    upkeep_day: str
    unpaid_days: int
    sold_total: int
    created: float
    dividend_rate: float


class StakeRow(TypedDict):
    """ほかの人の会社への出資（company_stakes 表の行）。"""

    guild_id: int
    owner_id: int
    investor_id: int
    amount: int
    dividends: int
