"""ソーシャル機能（P2P出品・抽選・投票・クイズ・地下作業・ショップ・会社）の行モデル。

対応する表の定義は infra/db/schema/tables.py。キーは `SELECT *` の列と同じ。
"""

from typing import Literal, NotRequired, TypedDict

from .games import AuctionHorseRow


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
    closed_at: float | None
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
    expires: float | None
    created: float


class LaborRow(TypedDict):
    """地下作業の状態（v4 で labor_* 7 表へ分離された内部結合ビューの型）。"""

    guild_id: int
    user_id: int
    pick: int
    glove: int
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
    clicks: int
    earned_total: int
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
    """「働く」を押した記録の1時間ごとの集計（labor_click_hours の読み出し射影）。"""

    hour: int
    clicks: int
    pay: int
    gaps: int
    sum_gap: float
    sum_gap2: float
    min_gap: float | None


class LaborWorkerRow(TypedDict):
    """雇っている作業員（labor_workers 表の行）。"""

    id: int
    guild_id: int
    user_id: int
    kind: str
    expires_at: float


class ShopStockRow(TypedDict):
    """ショップの在庫（shop_stock 表の行）。"""

    guild_id: int
    user_id: int
    item_id: str
    stock: float
    updated: float


class ProfileRow(TypedDict):
    """プロフィール（v4 で user_equipment・user_buffs へ分離された集約ビューの型）。"""

    guild_id: int
    user_id: int
    title: str | None
    card_color: str | None
    card_icon: str | None
    skin_dice: str | None
    skin_table: str | None
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


class LaborFindSummary(TypedDict):
    """最近の掘り出しの表示用（labor_finds の読み出し射影）。"""

    at: float
    kind: str
    ore: str


class RaffleView(RaffleRow):
    """募集中の抽選（抽選の行に、参加者一覧を足したもの）。"""

    entries: list[int]


class PollView(PollRow):
    """投票の表示用（投票の行に、本物の票数と表示する票数を足したもの）。"""

    voters: int
    yes: int
    no: int


class P2PMine(TypedDict):
    """自分の出品（出しているもの）と、自分が最高入札者の競り。"""

    selling: list[ListingRow]
    bidding: list[ListingRow]


class QuizView(TypedDict):
    """クイズの問題の表示用（choices はパース済みの配列、order は表示する並び）。"""

    id: int
    question: str
    choices: list[str]
    explanation: str
    shown_order: str
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
    expires: float | None
    created: float
    order: list[str]


class QuizAdminView(QuizView):
    """管理の一覧用の問題（回答数と正解数つき）。"""

    answered: int
    right_count: int


class RaffleResult(RaffleView):
    """締めた抽選の結果（募集中の抽選に、当選者・賞金・返金フラグを足したもの）。"""

    winner: int | None
    prize: int
    refunded: bool


class PollRigResult(TypedDict):
    """票を書き換えた結果（poll_rig の成功ペイロード）。"""

    poll: PollView  # 書き換え後の投票
    before: tuple[int, int]  # 書き換え前の (賛成, 反対)


class ShopStockInfo(TypedDict):
    """在庫のある品の、いまの在庫の状況（shop_stock の返り値）。"""

    stock: int  # 買える数（小数を切り上げた数）
    exact: float  # 正確な在庫（小数。時間で補充された分を含む）
    max: int
    next_at: float | None  # 次に1つ増える時刻（満タンなら None）


class ProfileView(TypedDict):
    """プロフィールの表示用（get_profile の返り値）。行がなければ guild_id キーは無い。"""

    user_id: int
    title: str | None
    card_color: str | None
    card_icon: str | None
    skin_dice: str | None
    skin_table: str | None
    daily_boost: int
    insurance: int
    status: str  # 身分（カタギ・借金など）
    frozen: bool  # 差し押さえ中か（カタギでないか）
    guild_id: NotRequired[int]


class AuctionListing(TypedDict):
    """帝愛の競売の棚の1品（auction_list の返り値の要素）。"""

    item_id: str
    qty: int  # 残りの数
    price: int  # いちばん安い（古い）出品の1つあたりの値段
    rate: float  # 定価に対する割合


class TeiaiMarketListing(TypedDict):
    """帝愛の出品の1件（market_list の返り値の要素）。"""

    id: int
    item_id: str
    mode: str
    qty: int
    black: bool  # 闇市品か
    price: int  # いまの1つあたりの値段
    rate: float  # 定価に対する割合
    expires_at: float


class CatalogSnapshot(TypedDict):
    """ショップのカタログ画面のスナップショット（services の catalog_snapshot の返り値）。"""

    inv: dict[str, int]  # 持ち物 {品のID: 個数}
    balance: int
    waiting: int  # 競売に出ている品と馬の合計（ホームタブ用）
    stocks: dict[str, ShopStockInfo]  # {品のID: 在庫}（在庫のある品だけ）


class AuctionSnapshot(TypedDict):
    """競売画面のスナップショット（services の auction_snapshot の返り値）。"""

    balance: int
    listed: list[AuctionListing]  # 差し押さえ品の競売
    market: list[TeiaiMarketListing]  # 帝愛の出品
    horses: list[AuctionHorseRow]  # 競売に出ている馬
    p2p_total: int  # プレイヤーの出品の総数
    tab: str  # 表示しているタブ（teiai / players）
    p2p_rows: list[tuple[ListingRow, str]]  # プレイヤーの出品 [(出品, 表示ラベル), ...]


class QuizImportItem(TypedDict):
    """まとめて登録する問題1件（quiz_add_many の items の要素）。"""

    question: str
    choices: list[str]  # 先頭が正解
    explanation: str
    reward: int
    minutes: int | None  # None なら無期限


class QuizAnswerResult(TypedDict):
    """回答の結果（quiz_answer の成功ペイロード）。"""

    correct: bool
    reward: int  # 実際に入った報酬
    answer: str  # 正解の選択肢
    explanation: str
    author_id: int
    author_delta: int  # 出題者の残高の変動
    author_total: int  # 出題者への累計の支払い
    author_debt_added: int  # 出題者の借金になった分
    reward_blocked: bool  # 正解だが、遊んだ回数が足りず報酬が出なかったか


class QuizSettleRow(QuizView):
    """決着した問題（quiz_settle / quiz_withdraw の返り値の要素。問題の表示用に成績を足したもの）。"""

    participants: int
    correct_count: int
    base: int  # 出題者への基本報酬
    bonus: int  # 正答率ボーナス
    part: int  # 参加報酬
    right_users: list[int]
    wrong_users: list[int]


class LaborState(TypedDict):
    """地下作業のいまの状態（labor_state の返り値）。行がまだ無ければ guild_id・user_id のキーは無い。"""

    pick: int
    glove: int
    dorm: int
    fatigue: float
    fatigue_at: float
    pot: int  # 受け取る前の稼ぎ
    pot_workers: int
    donated: int
    day: str
    day_earned: int
    day_click: int
    last_click: float
    clicks: int
    earned_total: int
    idle_at: float
    renew: int
    ores: dict[str, int]  # 決算ずみの鉱石 {鉱石: 個数}
    relics: dict[str, int]  # 決算ずみの発掘品 {品のID: 個数}
    apprentice: int
    miner: int
    craftsman: int
    foreman: int
    balance: int  # 手元の残高
    workers: int  # 雇っている作業員の数
    slots: int  # 雇える枠（寮の広さ）
    rate: float  # 作業員の稼ぎ（見込みを含む）/時
    idle_pending: int  # まだ数えていない作業員の稼ぎ（見込み）
    pot_total: int
    pot_workers_total: int
    renew_fee: int  # 切れていた契約の更新に、いま払う額
    expires: dict[str, float]  # {作業員の種類: いちばん早く切れる契約の期限}
    drinks: int  # 栄養ドリンコの手持ち
    finds: list[LaborFindSummary]  # 最近の掘り出し
    held_relics: dict[str, int]  # 持っている発掘品（未決算の分とは別）
    donated_all: int  # このサーバーの全員の寄付の合計
    terrain: str  # 今日の地層
    crit_chance: float
    guild_id: NotRequired[int]
    user_id: NotRequired[int]


class LaborTop(TypedDict):
    """採掘ランキング（labor_top の返り値）。"""

    rows: list[tuple[int, int, int, int]]  # (順位, ユーザーID, 稼ぎ, 押した回数)
    me: tuple[int, int, int, int] | None  # 自分の (順位, 稼ぎ, 押した回数, 人数)。まだ稼いでいなければ None


class LaborClickOk(TypedDict):
    """「働く」を1回押した結果（labor_click の成功の返り値）。"""

    ok: Literal[True]
    pay: int
    crit: bool  # 鉱脈ヒットか
    ore: str | None  # 手掘りで出た鉱石（なければ None）
    combo: bool  # コンボ達成か
    terrain: str  # 今日の地層


class LaborClickFail(TypedDict):
    """「働く」を押せなかった結果（labor_click の失敗の返り値）。"""

    ok: Literal[False]
    reason: str
    quiet: bool  # 静かに失敗したか（通知しないか）


LaborClick = LaborClickOk | LaborClickFail
"""「働く」を1回押した結果。`r["ok"] is True / is False` で成否の枝に絞れる。"""


class LaborCollectDetail(TypedDict):
    """受け取りの内訳（labor_collect(detail=True) の返り値）。"""

    amount: int  # 残高に移した額（借金の天引きの前）
    skimmed: int  # 帝愛のピンハネ（寄付）
    donated: int  # この人のこれまでの寄付の合計
    donated_all: int  # このサーバーの全員の寄付の合計
    ores: dict[str, int]  # 掘った鉱石
    relics: dict[str, int]  # 見つけた発掘品（持ち物に入った分）


class LaborClickReportRow(TypedDict):
    """1時間ごとの「働く」の集計の1行（click_report の rows の要素）。"""

    hour: int
    clicks: int
    pay: int
    mean_gap: float | None  # 押す間隔の平均（秒）
    cv: float | None  # 間隔の変動係数
    min_gap: float | None


class LaborClickReport(TypedDict):
    """「働く」の記録の画面用の集計（click_report の返り値）。"""

    clicks: int
    pay: int
    active_hours: int  # 押した時間の数
    longest_run: int  # 休まず押し続けた連続の時間数
    rows: list[LaborClickReportRow]


class CompanyView(CompanyRow):
    """会社の表示用（company_get / company_found の返り値。会社の行に、その日の状態と計算で出る数字を足したもの）。"""

    invested: int  # 建設費の累計
    daily_output: float  # 1日の生産量
    storage_cap: float  # 倉庫の容量
    upkeep: int  # 1日の維持費
    upgrade_cost: int | None  # 次の増築費（最大なら None）
    condition: str  # きょうの調子
    yield_mult: float
    price: float  # きょうの買取価格（倍率）
    stock_value: int  # 倉庫の在庫の評価額
    budget: int  # 帝愛のきょうの買取予算
    stakes_total: int  # 出資の合計
    stake_cap: int  # 出資の上限
    investors: int  # 出資者の数
    withdrawable: int  # 社長が引き出せる額


class CompanyShipResult(TypedDict):
    """出荷の結果（company_ship の成功ペイロード）。"""

    paid: int  # 会社資金に入った額
    pinhane: int  # 帝愛のピンハネ
    price: float  # きょうの買取価格
    left: int  # 残った在庫の評価額
    dividends: int  # 出資者へ配った合計


class StakeInvestResult(TypedDict):
    """出資の結果（stake_invest の成功ペイロード）。"""

    amount: int  # 今回の出資額
    total: int  # この会社への累計の出資額
    name: str  # 会社名


class StakeHolding(TypedDict):
    """自分の出資先1件（stake_holdings の返り値の要素）。"""

    owner_id: int
    name: str
    kind: str
    amount: int
    dividends: int  # これまでの配当の累計
    share: float  # 持分の割合
    dividend_rate: float


class CompanyBankrupt(TypedDict):
    """倒産した会社（company_settle_all の返り値の要素）。"""

    user_id: int
    name: str


class StreamRow(TypedDict):
    """配信（streams 表の行）。"""

    id: int
    guild_id: int
    streamer: int
    title: str
    url: str
    channel_id: int
    message_id: int
    started: float
    ends: float  # この時刻を過ぎたら、見回りで自動で閉じる
    tips: int  # スパチャの合計（配信者が受け取った額）
    tip_count: int
    status: str  # live / ended
    voice_channel_id: int  # 配信している Discord のボイスチャンネル（なければ 0）
    created_vc: int  # 1 なら Bot が作った VC（配信を閉じたら消す）


class StreamRoundView(TypedDict):
    """配信の勝負（stream_rounds 表の行）に、いまの賭けの合計を足したもの。"""

    id: int
    stream_id: int
    guild_id: int
    label: str
    status: str  # open（受付中）/ locked（締切）/ win / lose / void
    created: float
    win_total: int
    lose_total: int
    bettors: int


class StreamView(StreamRow):
    """配信の表示用（配信の行に、受付中か締切の勝負を足したもの。勝負がなければ round は None）。"""

    round: StreamRoundView | None


class StreamSettleResult(TypedDict):
    """勝負の精算の結果。winner が None なら無効（全額返した）。"""

    stream: StreamView
    round_id: int
    label: str
    winner: str | None
    payouts: dict[int, int]  # user_id → 返したペリカ（賭け金を含む）
    rake: int
    refunded: bool
