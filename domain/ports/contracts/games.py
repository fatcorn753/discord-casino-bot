"""ゲーム機能（競馬・レース・地下闘技場・特別イベント）の行モデル。

対応する表の定義は infra/db/schema/tables.py。キーは `SELECT *` の列と同じ。
"""

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from domain.horse import Runner, Track


class HorseRow(TypedDict):
    """馬主が持つ競走馬（horses・horse_ownership・horse_auction_listings の結合ビュー）。"""

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
    listed_at: float | None
    style: str | None


class RaceRow(TypedDict):
    """レース（horse_races 表の行）。"""

    id: int
    guild_id: int
    kind: str  # regular / special
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
    """出走馬（horse_entries・horse_race_registrations の共通投影行）。"""

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
    style: str | None
    form: int
    races: int
    wins: int
    places: int
    odds_win: float
    odds_place: float
    finish: int


class RaceBetRow(TypedDict):
    """馬券（horse_bets・horse_bet_picks の結合投影行）。"""

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
    result: str | None  # NULL 未決着 / won / lost


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


class PachinkoStats(TypedDict):
    """パチンコの集計（pachinko_stats の返り値）。"""

    omen: dict[str, tuple[int, int]]  # 予告の色ごとの (回転数, 当たり数)
    tier: dict[str, tuple[int, int]]  # リーチの格ごとの (回転数, 当たり数)
    spins: tuple[int, int]  # 全体の (回転数, 当たり数)
    sets: tuple[
        int, int, int, int, int, int, int
    ]  # (セット数, 当たりセット数, 賭け金合計, 払い戻し合計, 玉数, スタート数, 最大連チャン)


class RaceCloseResult(TypedDict):
    """出走馬の確定（race_close_entries の返り値）。"""

    runners: list["Runner"]  # 出走馬（NPC で埋めた分を含む）
    track: "Track"  # コース


class RaceBetResult(TypedDict):
    """買った馬券（race_bet の成功ペイロード）。"""

    id: int
    kind: str
    lane: int  # 単勝・複勝の枠番（組み合わせでは先頭）
    lanes: list[int]  # 選んだ枠番の並び
    stake: int
    odds: float
    name: str  # 馬名を区切りで並べた表示名


class RetiredHorseRow(HorseRow):
    """定年引退した馬（race_run の retired の要素。馬の行に引退金を足したもの）。"""

    value: int  # 引退金（マイナスなら引退費用）


class RaceRunResult(TypedDict):
    """レースを走らせた結果（race_run の返り値）。"""

    race: RaceRow
    entries: list[RaceEntryRow]  # 出走馬（finish が埋まったもの）
    order: list[int]  # 着順（entries の添字の並び）
    frames: list[list[float]]  # ティックごとの進み（演出用）
    finish: list[float]  # ゴール時刻（entries の添字順）
    bets: list[RaceBetRow]  # 精算ずみの馬券（payout が埋まったもの）
    prizes: dict[int, int]  # {オーナーID: 手取り賞金の合計}
    track: "Track"
    retired: list[RetiredHorseRow]  # 定年引退した馬
    is_major: bool  # 重賞か
    styles: dict[int, str]  # {枠番: 脚質}


class ArenaGroupResult(TypedDict):
    """準決勝の1組の結果（arena_run_semis の groups の値）。"""

    winner: str
    losers: list[str]


class ArenaSemiResult(TypedDict):
    """準決勝の全組の結果（arena_run_semis の返り値）。"""

    groups: dict[int, ArenaGroupResult]  # {組番号: 結果}
    finalists: list[str]  # 決勝へ進んだ戦士の名前


class ArenaFinalResult(TypedDict):
    """決勝の結果（arena_run_final の返り値）。"""

    champion: str
    runner_up: list[str]
