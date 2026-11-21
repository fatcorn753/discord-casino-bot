"""ゲーム機能（競馬・レース・地下闘技場・特別イベント）の行モデル。

対応する表の定義は infra/db/schema/tables.py。キーは `SELECT *` の列と同じ。
"""

from typing import Optional, TypedDict


class HorseRow(TypedDict):
    """馬主が持つ競走馬（horses 表の行）。"""

    id: int
    guild_id: int
    owner_id: int
    name: str
    coat: str
    silks: str
    speed: int
    stamina: int
    power: int
    guts: int
    races: int
    wins: int
    places: int
    earnings: int
    bonus: int
    created: float
    listed_at: Optional[float]
    rented_day: Optional[str]
    style: Optional[str]


class RaceRow(TypedDict):
    """レース（horse_races 表の行）。"""

    id: int
    guild_id: int
    kind: str  # regular / special
    grade: str
    start_at: float
    entry_close: float
    bet_close: float
    status: str  # entry / betting / done
    distance: int
    going: str
    channel_id: int
    message_id: int
    created: float


class RaceEntryRow(TypedDict):
    """出走馬（horse_entries 表の行）。"""

    id: int
    race_id: int
    lane: int
    horse_id: int
    owner_id: int
    name: str
    coat: str
    silks: str
    speed: int
    stamina: int
    power: int
    guts: int
    tactic: str
    style: Optional[str]
    form: int
    races: int
    wins: int
    places: int
    odds_win: float
    odds_place: float
    finish: int


class RaceBetRow(TypedDict):
    """馬券（horse_bets 表の行）。"""

    id: int
    guild_id: int
    race_id: int
    user_id: int
    kind: str  # win / place / quinella / wide / exacta / trio
    lane: int
    picks: str
    stake: int
    odds: float
    payout: int
    settled: int
    created: float


class ArenaTournamentRow(TypedDict):
    """地下闘技場の大会（arena_tournaments 表の行）。"""

    id: int
    guild_id: int
    day: str
    format: str  # duel（1on1）/ royale（バトロワ）
    status: str  # semi_betting -> semi_done -> final_betting -> done
    created: float


class ArenaTournamentDetailRow(ArenaTournamentRow):
    """大会の詳細（大会の行に、戦士一覧を足したもの）。"""

    fighters: list["ArenaFighterRow"]


class ArenaFighterRow(TypedDict):
    """闘技場の戦士（arena_fighters 表の行）。"""

    id: int
    tournament_id: int
    name: str
    base_power: int
    group_no: int
    stage: str  # semi / final / out
    bet_total: int
    result: Optional[str]  # NULL 未決着 / won / lost


class ArenaBetRow(TypedDict):
    """闘技場への賭け（arena_bets 表の行）。"""

    id: int
    tournament_id: int
    stage: str  # semi / final
    fighter_id: int
    user_id: int
    amount: int
    payout: int
    settled: int
    created: float


class ManualEventRow(TypedDict):
    """持ち主が開いた特別イベント（manual_event 表の行）。"""

    guild_id: int
    game: str
    boost: int
    starts: float
    ends: float
    ended: int
    channel_id: int


class AuctionHorseRow(HorseRow):
    """帝愛の競売に出ている馬（馬の行に、査定の価格と率を足したもの）。"""

    price: int
    rate: float
