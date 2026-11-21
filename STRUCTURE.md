# ファイル・フォルダの案内

このリポジトリの、どのファイル・フォルダが、何のためのものかをまとめた一覧です。
遊び方・コマンド・導入手順は [README.md](README.md)、各ゲーム・機能のくわしい仕様は [docs/](docs/) にあります。このファイルは、**コードの置き場所の地図**だけを書きます。

> ファイルを足す・名前を変えるときは、この一覧も直してください。

## 全体のつくり（トップレベル 4 層アーキテクチャ）

依存の向きは外側から内側（`cogs/` → `services/`・`domain/`、`infra/` → `domain/`）に揃え、各層の責務を分離しています。

| 層 | フォルダ | 役目 |
|---|---|---|
| プレゼンテーション | `cogs/` | スラッシュコマンド・ボタン・埋め込み・ゲーム進行演出（Discord に直接触るのはここだけ） |
| アプリケーション | `services/` | ユースケース・実行制御（二重プレイ防止など）。`domain/` の契約とルールにのみ依存 |
| ドメイン | `domain/` | 純粋な業務ルール・ゲーム計算（`games/`）・値型・水平ポート契約（`ports/`）。SQLite にも Discord にも触らない |
| インフラストラクチャ | `infra/` | SQLite 永続化（`db/`）・YAML カタログ読み込み（`catalogs/`）・時刻・パス解決（`paths.py`） |
| 設定カタログ | `catalogs/` | 商品・確率表・配当表などの UI 非依存なゲームバランス/マスタ定義（YAML）。`infra/catalogs/` で型検証して読み込む |

## 最上位

| パス | 内容 |
|---|---|
| `main.py` | Composition Root（Bot 起動点）。設定を読み、`infra` と `services` を組み立て、`cogs/loader.py` で Cog を読み込む |
| `manage.py` | Composition Root（持ち主向け CLI 管理ツール）。`grant` でペリカ付与、`servers` で許可状況参照（`discord.py` 非依存） |
| `config.example.json` | 設定ファイルの見本（トークンなどは空）。`config.json` を作るときのひな形 |
| `config.json` | 実際の設定（Bot のトークンなど）。**秘匿情報なので Git に入れない**（`.gitignore` 済み） |
| `config.json.migrated-to-server` | 古い設定ファイルの控え。`config.json.*` として Git に入れない |
| `catalogs/` | YAML の静的カタログ群（ショップ・スキン・遺物・債券・ミッション・イベント・地下作業・競馬・証券・帝愛・経済・ゲーム・会社・闘技場） |
| `data/` | 実行時のデータ。`casino.db`（SQLite 本体）と `bot.lock`（二重起動の防止）。**Git に入れない** |
| `README.md` | 概要・コマンド一覧・導入手順・運用・docs への案内 |
| `docs/` | 機能ごとの、くわしい仕様（`casino.md`・`stocks.md`・`teiai.md`・`horse.md`・`labor.md`・`arena.md`・`shop.md`・`admin.md`・`company.md`） |
| `STRUCTURE.md` | このファイル |
| `pyproject.toml` / `uv.lock` | Python のプロジェクト設定・ruff・pyright・pytest 設定と、依存の固定（uv） |
| `requirements.txt` / `requirements-dev.txt` | 実行に必要な依存 / 開発（テスト・lint・型検査）に必要な依存 |
| `LICENSE` | ライセンス |
| `.gitignore` | Git に入れないものの一覧（設定・DB・ログ・キャッシュなど） |
| `tests/` | 自動テスト（`domain/`・`infra/`・`cogs/`・`flows/`） |

## `cogs/` — コマンドと画面

`loader.py` に列挙された `*_cog.py` / `cog.py` が Discord に読み込まれる Cog です。静的文言テーブル・AA・ラベル定義は各サブパッケージの `texts.py`（プレゼンテーション静的テキスト層）に分離しています。

| パス | 内容 |
|---|---|
| `loader.py` | 読み込む Cog モジュール一覧（`COG_MODULES`）と `load_all_cogs()`（`main.py` とテストで共有） |
| `shared/` | 共通 Discord 基盤（`bot.py` / `bridges.py` / `ui.py` / `texts.py` / `tasks.py` / `confirm.py` / `messages/` / `approval_guard.py` / `ranking.py` / `announce.py` / `amount.py` / `horse_display.py` / `logthrottle.py`）。`CasinoBot` と Cog 間ブリッジ（`bridges.py`: `BotBridges`・`resolve_bridge` / `require_bridge`・名目的基底クラス群）により Service Locator なしで疎結合に連携、`tasks.py` で承認済みギルド走査を共通化、`logthrottle.py` で同じ原因の警告ログを間引く。`cogs/` は `infra/` を import しない（ログは標準 `logging`） |
| `casino/` | `/カジノ`。`cog.py` と進行演出・静的台本（`common.py`・`simple.py`・`blackjack.py`・`chinchiro.py`・`pachinko.py`・`texts.py`） |
| `economy/` | `/ペリカ`。`cog.py`・`embeds.py`（残高・デイリー・返済・破産・ミッション・履歴の画面）・`boards.py`（ランキング・掲示板自動更新） |
| `teiai/` | `/ペリカ 帝愛` のパネル（`panel.py`）と海外逃亡の演出（`escape.py`）、静的 AA・状態文言（`texts.py`） |
| `kyosai/` | `/共済`。帝愛共済（#34）の持ち口の譲渡・競り・動議と配当（`cog.py`） |
| `poll/` | `/投票`。誰でも作れる賛成・反対の二択投票（`cog.py`・`views.py`） |
| `shop/` | `/ショップ`。`cog.py`・`actions.py`・`embeds.py`・`texts.py`・`views.py`・`discord_ops.py`（ロール・ニックネーム反映） |
| `horse/` | `/競馬`。`cog.py`・`cards.py`・`betting.py`・`stable.py`・`views.py`（ファサード） |
| `market/` | `/証券` と債券定期処理。`cog.py`・`feed.py`・`bond_cog.py`・`embeds.py`・`bond_embeds.py`・`orders.py`・`views.py` |
| `loan/` | `/貸付`。ユーザー同士の貸し借り（`cog.py`・`embeds.py`・`views.py`） |
| `p2p/` | プレイヤー間の市場と競り（`cog.py`・`embeds.py`・`views.py`） |
| `labor/` | `/地下 作業`・`/地下 ランキング`。クリッカー、作業員の雇用、鉱石の掘り出し（`cog.py`・`embeds.py`・`views.py`） |
| `company/` | `/会社`。会社の設立・事業所の増築・出荷・維持費（`cog.py`・`embeds.py`・`texts.py`・`views.py`） |
| `arena/` | `/闘技場`。地下闘技場。`cog.py`（進行・掲示）・`views.py`（賭けパネル）・`embeds.py`。**再開済み（#88）**: `loader.py` の `COG_MODULES` に含まれている |
| `quiz/` | `/クイズ`。`cog.py`・`embeds.py`・`texts.py`・`views.py`（4 択クイズと報酬） |
| `raffle/` | 抽選。参加費を預かり、締切で 1 人に全額を渡す（`cog.py`・`embeds.py`・`views.py`） |
| `event/` | 突発イベントの告知（`cog.py`・`texts.py`、1 日 1 回、30 分だけ勝ち分が増える） |
| `weekly/` | 週間ランキングの自動発表（毎週月曜 0 時）と限定称号の付与（`cog.py`・`embeds.py`） |
| `admin/` | サーバー承認と `/運営` コマンド（`cog.py`・`approval_ops.py`・`economy_ops.py`・`content_ops.py`・`feature_ops.py`・`embeds.py`・`texts.py`・`views.py`・`purge.py`・`targets.py`） |
| `help/` | `/help`。カテゴリ別の案内（`cog.py`・`texts.py`） |

## `services/` — アプリケーションサービス

`cogs/` と `domain/`・`infra/` を仲介し、ユースケース単位のトランザクション・時刻取得・状態遷移を調停します（Constructor DI でリポジトリ・ポートを受け取り、`: Any` 抜け道や Service Locator を持たない）。

**Cog は Repository を持たない。** 状態を変える処理・時刻を伴う処理・表示用の読み取りは、すべて Service のメソッドを通す。例外は `IGuildRepo`（サーバーの許可・告知先・表示の設定。全 Cog が共有する Discord 側の設定）だけで、`tests/infra/test_architecture_ports.py` が守る。Service は Repository を公開属性にしない（`svc.wallet.for_guild(...)` のような素通りを作らない）。

| パス | 内容 |
|---|---|
| `container.py` / `__init__.py` | `ServiceRegistry`、`build_services()` |
| `session.py` | `Sessions(ISessionGuard)`。同じ人が同時に複数のゲームを始めないようにする排他制御 |
| `casino.py` | `CasinoService`。カジノ開始検証・escrow預かり・RTP/イベント補正・精算・記録 |
| `market.py` | `MarketService`。市場価格進行・ボラティリティ/決算更新・保有スナップショット |
| `horse.py` | `HorseService`。定時レース生成・締切オッズ確定・発走精算の調整。`desk(guild_id)` でサーバーごとの競馬窓口 `HorseDesk`（出走表・馬券・厩舎）を返す |
| `economy.py` | `EconomyService`。デイリー受取・送金・残高カード・ミッション・履歴・週間ランキングの発表と補完 |
| `labor.py` | `LaborService`。地下作業の操作（働く・受け取る・雇用・強化・発掘品の合体）・ランキング・クリック履歴レポート |
| `arena.py` | `ArenaService`。闘技場トーナメント生成・進行時刻取得・準決勝/決勝処理の調停 |
| `loan.py` | `LoanService`。個人間貸付（提案・募集・承諾・辞退・取り下げ・返済・期日チェック）の調停 |
| `company.py` | `CompanyService`。会社の設立・出資・引き出し・出荷・増築・種類の変更・定時精算 |
| `teiai.py` | `TeiaiService`。帝愛への返済・種銭・自己破産・海外逃亡・帝愛銀行（預金・CDS）・破産猶予の時間切れ |
| `kyosai.py` | `KyosaiService`。帝愛共済（#34）の持ち口の照会・譲渡・競り・動議・配当の調停 |
| `ranking.py` | `RankingService`。ランキング描画の読み取り専用の問い合わせ（`for_guild` でサーバーを絞る） |
| `quiz.py` | `QuizService`。クイズの出題・挑戦・回答・取り下げ・期限の精算・運営の追加と削除 |
| `raffle.py` | `RaffleService`。抽選の募集・参加・中止・締切の精算 |
| `p2p.py` | `P2PService`。プレイヤー間の出品・購入・入札・取り消し・締切の精算 |
| `poll.py` | `PollService`。投票（/投票）の作成・投票・票数の書き換え・締切の精算 |
| `event.py` | `EventService`。突発イベントの告知状態と、持ち主が開く特別イベント |
| `admin.py` | `AdminService`。運営の付与・残高の設定・還元率・パチンコの集計 |
| `shop.py` | `ShopService`。ショップ・帝愛の競売・持ち物・称号解決の調停 |

## `domain/` — 純粋なルール・値型・ポート契約

### `ports/` — DIP 水平インターフェース（名目的基底クラス群）

| パス | 内容 |
|---|---|
| `catalogs.py` / `catalog_types/` | `ICatalogs` カタログ取得契約と責務別公称データ契約（`shop`・`economy`・`market`・`games`・`arena`・`company`） |
| `clock.py` | `IClock`（現在時刻・日付・UTCオフセット・タイムスタンプの抽象契約） |
| `session.py` | `ISessionGuard`（二重プレイ防止セッション管理の契約） |
| `repos/` | 領域別 DB リポジトリ用名目的基底クラス群（`base.py`・`economy.py`・`teiai.py`・`market.py`・`social.py`・`games.py`・`bundle.py` / `__init__.py` で `IRepositoryBundle` 等を集約再エクスポート） |
| `contracts.py` | ポートの戻り値に使う型付きの値型（`ActiveBond`・`SeizedBelongings` 等。catalogs 非依存） |
| `bridges/` | リポジトリ・サービス間の循環を防ぐ細粒度ブリッジ契約と Null Object / Composite。責務別サブモジュール（`debt.py`・`bank.py`・`margin.py`・`seizure.py`・`loans.py`）に分割し、`__init__.py` で全シンボル（`NoOp*`・`Composite*`・`IValuationSource`・`IAssetCollector`・`IAssetSeizureStep`・`AddAssetPart` 等）を再エクスポート |

### `games/` — ゲームのルール（純粋な計算）

| パス | 内容 |
|---|---|
| `slot.py` | スロット。出目の重み・払い戻し・還元率の計算 |
| `roulette.py` | ルーレット。賭け方と払い戻し |
| `blackjack.py` | ブラックジャックのルール |
| `chinchiro.py` | チンチロの役と勝敗 |
| `pachinko.py` | パチンコ。当たりの確率、リーチ・予告の抽選、還元率の計算 |
| `rtplevel.py` | 還元率のレベル（1〜6）と `MIN_BET` |

### ドメインモジュール（DB・Discord 非依存）

| パス | 内容 |
|---|---|
| `types.py` | 結果データクラス（`DailyResult` 等）と共有定数（`INITIAL_BALANCE`・`DAILY_*`・`asset_groups` 等） |
| `errors.py` | 構造化エラー・通知イベント（`Failure`・`Notice`・`fail`・`notice`） |
| `approval.py` | サーバー許可リストと承認状態の純粋判定 |
| `features.py` | 機能のオン・オフ（サーバーごと。#99）。止めるのは「新しく始めること」だけで、進行中のものはいつもどおり精算する |
| `nick.py` | 称号をニックネームの後ろに付け外しする文字列処理 |
| `relics.py` | 発掘品の効果（持っている間、種類ごとに +1%） |
| `labor.py` | 地下作業の数字・鉱石の表・抽選・契約の決済の計算 |
| `horse.py` | 競馬の数字とレースのシミュレーション |
| `arena.py` | 地下闘技場の数字（戦士生成・武器強度・試合シミュレーション・オッズ・配当スケール） |
| `market.py` / `market_events.py` | 証券の価格変動・売買計算・チャート / ボラティリティ予報と相関崩壊警報 |
| `options.py` | オプションの値付け・銘柄表示名 |
| `bonds.py` | 債券の種類・利率・期間 |
| `rates.py` | 金利の共通計算（1日あたり・連続複利。#33・#18。r_f は帝愛銀行の準備率に連動） |
| `teiai.py` | 帝愛ファイナンス（借金・身分・銀行）の数字 |
| `teiai_market.py` | 帝愛の期間限定出品（#72）。1日の出品計画・競売の値下がり・闇市品の開封（乱数は呼び出し側から渡す） |
| `kyosai.py` | 帝愛共済（#34）の数字と計算（固定総口数の持分・配当・超過損失の負担） |
| `company.py` | 会社の数字（建設費・生産と倉庫・日替わりの状態と相場・出荷額・維持費） |
| `thermostat.py` | 帝愛銀行の最前線サーモスタット（熱量と器の時間推移・符号付きの熱量・分割不変な流入/損失の配分・バッチの純額） |
| `loans.py` | 貸し借りの利息計算（日割りの単利） |
| `p2p.py` | プレイヤー間取引のルール（値段幅・手数料・入札幅・延長） |
| `poll.py` | 投票（/投票）の数字と、表示する票数の計算（書き換えの上限など） |
| `shop.py` | ショップの商品カタログ |
| `skins.py` | ゲーム演出スキン定義 |
| `missions.py` / `events.py` / `weekly.py` / `earnings.py` | デイリーミッション / 突発イベント / 週間集計 / 決算ジャンプ |
| `history.py` | ペリカの増減履歴の行整形 |
| `quizimport.py` / `quizpay.py` / `duration.py` | クイズ一括入力 / 出題報酬 / 期間文字列パース |

## `infra/` — 技術詳細・永続化・I/O

| パス | 内容 |
|---|---|
| `paths.py` | ルート・`catalogs/`・`config.json`・`data/casino.db` の基準パス解決 |
| `catalogs/` | `catalogs/*.yaml` を責務別に読み込み・Pydantic 型検証・キャッシュするパッケージ（`loader.py`・`shop.py`・`economy.py`・`games.py`・`market.py`・`arena.py`・`company.py`、`YamlCatalogs(ICatalogs)`） |
| `clock.py` | `ConfigClock(IClock)` とタイムゾーンに基づく現在時刻・日付の取得 |

### `infra/db/` — SQLite 永続化・独立リポジトリ群

| パス | 内容 |
|---|---|
| `base.py` | `EcoDb`（共有 DB 接続・ロック・`tx()`・`close()`）、`ScopedRepo`（ギルドスコープ共通基底）、`validate_guild_id()` |
| `schema/` | DB の表定義（`tables.py`）と旧版からの列追加・データ移行（`migrations.py`） |
| `repositories.py` | `RepositoryBundle` と `open_repositories()`（全リポジトリの構築・ブリッジ接続・`for_guild(gid)` スコープ切り替え・`db` 管理） |
| `lazy_bridges.py` | 初期化時の循環参照を解決する遅延解決ブリッジラッパー（`Lazy*`） |
| `account.py` | `AccountCore`（口座残高・台帳記帳・借金ライフサイクルフックの共有プリミティブ） |
| `escrow.py` / `wallet.py` | 賭け金預かり・精算（`EscrowRepo`）と残高・デイリー・ランキング・統計・送金（`WalletRepo`） |
| `assets.py` / `auction.py` / `bank.py` / `bonds.py` / `company.py` / `crypto.py` / `debt.py` / `earnings.py` / `events.py` / `guild.py` / `horse.py` / `kyosai.py` / `labor.py` / `loans.py` / `margin.py` / `market.py` / `market_events.py` / `notices.py` / `options.py` / `p2p.py` / `pachinko.py` / `poll.py` / `quiz.py` / `race.py` / `raffle.py` / `records.py` / `relics.py` / `rtp.py` / `shop.py` / `arena.py` | 機能別の独立リポジトリ群と `IAssetCollector` / `IAssetSeizureStep` / `ILiquidationStep` 戦略群（各 `domain/ports/repos/` 契約を継承実装） |

## `tests/` — 自動テスト

`uv run pytest -q` で実行します。本体の 4 層構成に対応したディレクトリに分かれています。

| パス | 内容 |
|---|---|
| `conftest.py` | テスト共通の準備（`ScopedRepo.for_guild`・`RepositoryBundle.for_guild` シングルスコーピング、ミッション・イベント固定など） |
| `support.py` | テスト共有ビルダー（`make_test_bot`・`make_*_cog`・`make_casino_deps` 等） |
| `domain/` | 純粋ドメインロジック・`domain/games/` の単体テスト |
| `infra/` | `catalogs/*.yaml` 検証・DB 永続化・`RepositoryBundle` ギルド分離・ポート境界＆規約ガード（`test_architecture_ports.py`）・Bridge OCP/ISP/LSP 検証（`test_bridge_ocp_lsp.py`）・銀行・証拠金・P2P 等のテスト |
| `cogs/` | Cog・埋め込み・モーダル・承認・共通 UI 部品のテスト |
| `flows/` | コマンド一覧・画面フロー全体の統合検証（`test_flows.py`） |

## 変更するとき、どこを見るか

- 数字・商品・確率・ヘルプ定義などを調整したい: `catalogs/*.yaml`（型検証は `infra/catalogs/`）または `domain/`。
- 画面・文言・Discord 操作を変えたい: `cogs/` 配下の各機能サブパッケージと `cogs/shared/`。
- DB の表や列を足したい: `infra/db/schema/` と対応する `infra/db/*.py`。
- 新しい機能を足す: ルールと契約を `domain/`、永続化を `infra/db/`、コマンドと UI を `cogs/`、テストを `tests/` に置く。
