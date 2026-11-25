"""ポートの戻り値に使う、型付きの入れ物のうち基礎の値型（catalogs に依存しない値型だけを置く）。

domain/types.py は catalogs を読むため、ポート（domain/ports）からは実行時に import できない。
ポートの契約に出てくる値型は、このパッケージに置く（行モデルは social / games / market / teiai の各サブモジュール）。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, NotRequired, TypedDict, TypeVar

from .teiai import CdsRow

if TYPE_CHECKING:
    from domain.missions import Mission

RowT = TypeVar("RowT")


@dataclass(frozen=True)
class ActiveBond:
    """持っている（status='active' の）債券 1 件。破産の換金・差し押さえで使う。"""

    id: int
    kind: str
    principal: int


@dataclass(frozen=True)
class SeizedBelongings:
    """破産で没収したショップの持ち物（品 ID と個数の列。持ち物 → デイリー2倍券 → 保険券の順）。"""

    items: tuple[tuple[str, int], ...]


# ------------------------------------------------------------------ #
# 照会系の戻り値（キーが決まった辞書は TypedDict、位置で読むものは tuple）#
# ------------------------------------------------------------------ #
SpotHolding = tuple[int, int]
"""現物の保有 (口数, 買うのにかかった総額)。"""

MarginPosition = tuple[int, float, int]
"""信用の建玉 (口数, 建てた総額, 担保)。建てていなければ (0, 0.0, 0)。"""

PricePoint = tuple[float, float, int]
"""価格の記録 (時刻, 価格, 出来事)。"""

RankEntry = tuple[int, int, int]
"""ランキングの位置 (順位, 額, 対象人数)。"""

WeeklyRankEntry = tuple[int, int, int, int]
"""週間ランキングの位置 (順位, 稼ぎ, ゲーム数, 人数)。"""


class HouseAccount(TypedDict):
    """帝愛物産（`/会社` の生産・出荷買取専用プール）。"""

    balance: int
    skimmed: int  # 後方互換用の累計列（現在は未使用）


class BankThermostat(TypedDict):
    enforce: bool
    load: float
    capacity: float
    room: float
    burned: float
    bailed: float


class BondQuote(TypedDict):
    """いま債券を買うときの見積もり（#18）。"""

    rate: float  # 満期までの利率（％。買ったときに固定）
    payout: int  # 満期の受取額
    sale: int  # 買ってすぐ売ったときの手取り（売れない債券は 0）


class BankStatus(TypedDict):
    """帝愛銀行の状態（表示用）。"""

    cash: float
    loaned: float
    deposits: int
    bonds: int  # 帝愛社債の元本残高（銀行の負債）
    locks: float
    available: float
    ratio: float | None  # available ÷ (信託総額 + 社債の元本残高)（どちらもなければ None）
    level: str  # safe / warn / danger / frozen
    thermostat: BankThermostat
    trust_rate: float  # 帝愛信託の利率（1日あたり。r_f + 上乗せ）
    risk_free: float  # 無リスク金利 r_f（1日あたり）
    rf_target: float  # いまの準備率での r_f の目標（1日あたり。#18）
    trust_paid: float  # これまでに払った信託の利息の累計
    excess_unpaid: float  # 信託でも熱浴・共済でも払えなかった超過損失の累計


class LoanCredit(TypedDict):
    """借りる人の信用情報（資産の額は含めない）。"""

    status: str
    debt: int
    grace_until: float | None
    repaid: int
    overdue: int
    discharged: int
    active: int
    owed: int
    bankrupt_at: float | None


class LoanBook(TypedDict, Generic[RowT]):
    """自分が借りている／貸している、終了前の貸付（loans 表の行）。"""

    borrowed: list[RowT]
    lent: list[RowT]


class CdsQuote(TypedDict):
    odds: float
    status: str
    debt: int
    grace: bool


class CdsHoldings(TypedDict):
    bought: list[CdsRow]  # 自分が買っている有効な CDS（cds 表の行）
    on_me: int  # 自分に掛けられている件数


class MarginStatus(TypedDict):
    """信用口座の維持率。建玉がなければ ratio は None。"""

    equity: float
    required: float
    collateral: float
    ratio: float | None


class HoldingBook(TypedDict, Generic[RowT]):
    """債券・オプションの一覧（表の行）。保有中（期限が近い順）と、直近に決着したもの（新しい順）。"""

    active: list[RowT]
    done: list[RowT]


class UserRow(TypedDict):
    """ユーザーの口座・デイリー・成績・借金・破産の結合射影。"""

    guild_id: int
    user_id: int
    balance: int
    daily_streak: int
    last_daily: str | None
    games_played: int
    total_wagered: int
    total_won: int
    biggest_win: int
    created_at: float
    last_slot: int | None
    debt: int  # 帝愛からの借金（シニア債権）
    debt_principal: int  # 借金の元本（利息の上限計算用）
    grace_penalty: float  # 破産猶予ペナルティ
    grace_penalty_at: float  # 最後に破産猶予に入った時刻
    bankrupt_at: float | None
    blacklist_until: float | None


class LedgerEntry(TypedDict):
    """台帳の1行（ledger 表の読み出し射影）。"""

    delta: int
    balance_after: int
    reason: str
    ts: float
    kind: NotRequired[str]
    counterparty_id: NotRequired[int | None]


class PlaySummary(TypedDict):
    """ゲームの成績のまとめ（play_state の読み出し射影）。"""

    plays: int
    wins: int
    net_total: int
    best_streak: int


class PlayRecord(TypedDict):
    """1回遊んだ結果の記録（record_play の返り値）。"""

    streak: int  # いまの連勝
    new_best: bool
    completed: list[tuple["Mission", int]]  # 達成したミッションと、その報酬
    bonus: int  # 全ミッション達成ボーナス


class WeeklyAward(TypedDict):
    """週間ランキングの発表の結果（weekly_award の返り値）。"""

    champion: tuple[int, int, int] | None  # 王者 (ユーザーID, 稼ぎ, ゲーム数)。いなければ None
    previous: list[int]  # 称号を外された人のユーザーID
    wins: int  # 王者の通算優勝回数
    standings: list[tuple[int, int, int]]  # (ユーザーID, 稼ぎ, ゲーム数) の並び


class GuildStatusRow(TypedDict):
    """サーバーの承認状態（guild_status 表の行）。"""

    guild_id: int
    status: str  # pending / approved / denied
    name: str | None
    decided_by: int | None
    updated_at: float
    notified_at: float | None


class LeaderboardRow(TypedDict):
    """ランキング掲示板の設定（leaderboards 表の行）。"""

    guild_id: int
    channel_id: int
    message_id: int
