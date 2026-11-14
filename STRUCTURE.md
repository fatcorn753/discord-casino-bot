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
| インフラストラクチャ | `infra/` | SQLite 永続化（`db/`）・YAML カタログ読み込み（`catalogs.py`）・時刻・パス解決（`paths.py`） |
| 設定カタログ | `catalogs/` | 商品・スキン・確率表・配当表・ヘルプ文面などの静的な定義（YAML）。`infra/catalogs.py` で型検証して読み込む |

## 最上位

| パス | 内容 |
|---|---|
| `main.py` | Composition Root（Bot 起動点）。設定を読み、`infra` と `services` を組み立て、`cogs/loader.py` で Cog を読み込む |
| `manage.py` | Composition Root（持ち主向け CLI 管理ツール）。`grant` でペリカ付与、`servers` で許可状況参照（`discord.py` 非依存） |
| `config.example.json` | 設定ファイルの見本（トークンなどは空）。`config.json` を作るときのひな形 |
| `config.json` | 実際の設定（Bot のトークンなど）。**秘匿情報なので Git に入れない**（`.gitignore` 済み） |
| `config.json.migrated-to-server` | 古い設定ファイルの控え。`config.json.*` として Git に入れない |
| `catalogs/` | YAML の静的カタログ群（ショップ・スキン・遺物・債券・ミッション・イベント・地下作業・競馬・証券・帝愛・経済・ゲーム・ヘルプ） |
| `data/` | 実行時のデータ。`casino.db`（SQLite 本体）と `bot.lock`（二重起動の防止）。**Git に入れない** |
| `README.md` | 概要・コマンド一覧・導入手順・運用・docs への案内 |
| `docs/` | 機能ごとの、くわしい仕様（`casino.md`・`stocks.md`・`teiai.md`・`horse.md`・`labor.md`・`shop.md`・`admin.md`） |
| `STRUCTURE.md` | このファイル |
| `pyproject.toml` / `uv.lock` | Python のプロジェクト設定・ruff・pyright・pytest 設定と、依存の固定（uv） |
| `requirements.txt` / `requirements-dev.txt` | 実行に必要な依存 / 開発（テスト・lint・型検査）に必要な依存 |
| `LICENSE` | ライセンス |
| `.gitignore` | Git に入れないものの一覧（設定・DB・ログ・キャッシュなど） |
| `tests/` | 自動テスト（`domain/`・`infra/`・`cogs/`・`flows/`） |

## `cogs/` — コマンドと画面

`loader.py` に列挙された `*_cog.py` / `cog.py` が Discord に読み込まれる Cog です。

| パス | 内容 |
|---|---|
| `loader.py` | 読み込む Cog モジュール一覧（`COG_MODULES`）と `load_all_cogs()`（`main.py` とテストで共有） |
| `shared/` | 共通 Discord 基盤（`bot.py` / `ui.py` / `confirm.py` / `approval_guard.py` / `ranking.py` / `announce.py` / `amount.py` / `horse_display.py`） |
| `casino/` | `/カジノ`。`cog.py` と進行演出（`common.py`・`simple.py`・`blackjack.py`・`chinchiro.py`・`pachinko.py`） |
| `economy/` | `/ペリカ`。残高・デイリー・ランキング（掲示板の自動更新を含む）・送金・履歴 |
| `teiai/` | `/ペリカ 帝愛` のパネル（`panel.py`）と海外逃亡の演出（`escape.py`） |
| `shop/` | `/ショップ`。`cog.py`・`embeds.py`・`views.py`・`discord_ops.py`（ロール・ニックネーム反映） |
| `horse/` | `/競馬`。`cog.py`・`views.py`（出馬表・厩舎・馬券のボタンとモーダル） |
| `market/` | `/証券` と債券定期処理。`cog.py`・`bond_cog.py`・`embeds.py`・`bond_embeds.py`・`orders.py`・`views.py` |
| `loan/` | `/貸付`。ユーザー同士の貸し借り |
| `p2p/` | プレイヤー間の市場と競り（`cog.py`・`embeds.py`） |
| `labor/` | `/地下 作業`・`/地下 ランキング`。クリッカー、作業員の雇用、鉱石の掘り出し |
| `quiz/` | `/クイズ`。4 択クイズと報酬 |
| `raffle/` | 抽選。参加費を預かり、締切で 1 人に全額を渡す |
| `event/` | 突発イベントの告知（1 日 1 回、30 分だけ勝ち分が増える） |
| `weekly/` | 週間ランキングの自動発表（毎週月曜 0 時）と限定称号の授与 |
| `admin/` | サーバー承認と `/運営` コマンド（`cog.py`・`embeds.py`・`views.py`・`purge.py`・`targets.py`） |
| `help/` | `/help`。カテゴリ別の案内 |

## `services/` — アプリケーションサービス

| パス | 内容 |
|---|---|
| `session.py` | `Sessions(ISessionGuard)`。同じ人が同時に複数のゲームを始めないようにする排他制御 |

## `domain/` — 純粋なルール・値型・ポート契約

### `ports/` — DIP / ISP 水平インターフェース

| パス | 内容 |
|---|---|
| `clock.py` | `IClock`（現在時刻・日付の抽象契約） |
| `session.py` | `ISessionGuard`（二重プレイ防止セッション管理の契約） |
| `economy.py` | `IGuildScopable` / `IGuildApprovalStore` / `IRankingReader` / `IEscrowPort` / `IWalletPort` / `IEconomy` |

### `games/` — ゲームのルール（純粋な計算）

| パス | 内容 |
|---|---|
| `slot.py` | スロット。出目の重み・払い戻し・還元率の計算 |
| `roulette.py` | ルーレット。賭け方と払い戻し |
| `blackjack.py` | ブラックジャックのルール |
| `chinchiro.py` | チンチロの役と勝敗 |
| `pachinko.py` | パチンコ。当たりの確率、リーチ・予告の設計、演出の台本、還元率の計算 |
| `rtplevel.py` | 還元率のレベル（1〜6）と `MIN_BET` |

### ドメインモジュール（DB・Discord 非依存）

| パス | 内容 |
|---|---|
| `types.py` | 結果データクラス（`DailyResult` 等）と共有定数（`INITIAL_BALANCE`・`DAILY_*`・`PART_LABELS` 等） |
| `approval.py` | サーバー許可リストと承認状態の純粋判定 |
| `nick.py` | 称号をニックネームの後ろに付け外しする文字列処理 |
| `relics.py` | 発掘品の効果（持っている間、種類ごとに +1%） |
| `labor.py` | 地下作業の数字・鉱石の表・抽選・契約の決算の計算 |
| `horse.py` | 競馬の数字とレースのシミュレーション |
| `market.py` / `market_events.py` | 証券の価格変動・売買計算・チャート / ボラティリティ予告と相関崩壊警報 |
| `options.py` | オプションの値付け・銘柄表示名 |
| `bonds.py` | 債券の種類・利率・期間 |
| `teiai.py` | 帝愛ファイナンス（借金・身分・銀行）の数字 |
| `loans.py` | 貸し借りの利息計算（日割りの単利） |
| `p2p.py` | プレイヤー間取引のルール（値段幅・手数料・入札幅・延長） |
| `shop.py` | ショップの商品カタログ |
| `skins.py` | ゲーム演出スキン定義 |
| `missions.py` / `events.py` / `weekly.py` / `earnings.py` | デイリーミッション / 突発イベント / 週間集計 / 決算ジャンプ |
| `history.py` | ペリカの増減履歴の行整形 |
| `quizimport.py` / `quizpay.py` / `duration.py` | クイズ一括入力 / 出題報酬 / 期間文字列パース |

## `infra/` — 技術詳細・永続化・I/O

| パス | 内容 |
|---|---|
| `paths.py` | ルート・`catalogs/`・`config.json`・`data/casino.db` の基準パス解決 |
| `catalogs.py` | `catalogs/` 配下の YAML を読み込み・Pydantic 型検証・キャッシュする窓口 |
| `clock.py` | `ConfigClock(IClock)` とタイムゾーンに基づく現在時刻・日付の取得 |
| `logthrottle.py` | 同じ原因のログを出し続けないための間引き |

### `infra/db/` — SQLite 永続化・`Economy` 実装

| パス | 内容 |
|---|---|
| `base.py` | `EcoDb`（共有 DB 接続・ロック）と全 Mixin が継承する型付き基底クラス `EcoBase` |
| `schema.py` | DB の表定義（作成）と旧版からの列追加・データ移行 |
| `economy.py` | `Economy(IEconomy)` 本体（残高・デイリー・送金・ランキング） |
| `assets.py` / `auction.py` / `bank.py` / `bonds.py` / `crypto.py` / `debt.py` / `earnings.py` / `events.py` / `guild.py` / `horse.py` / `labor.py` / `loans.py` / `market_events.py` / `options.py` / `p2p.py` / `pachinko.py` / `quiz.py` / `raffle.py` / `records.py` / `rtp.py` / `shop.py` | 機能別の DB 操作 Mixin 群 |

## `tests/` — 自動テスト

`uv run pytest -q` で実行します。本体の 4 層構成に対応したディレクトリに分かれています。

| パス | 内容 |
|---|---|
| `conftest.py` | テスト共通の準備（`Economy.for_guild` シングルスコープ、ミッション・イベント固定など） |
| `domain/` | 純粋ドメインロジック・`domain/games/` の単体テスト |
| `infra/` | `catalogs/*.yaml` 検証・DB 永続化・`Economy` ギルド分離・銀行・証拠金・P2P 等のテスト |
| `cogs/` | Cog・埋め込み・モーダル・承認・共通 UI 部品のテスト |
| `flows/` | コマンド一覧・画面フロー全体の統合検証（`test_flows.py`） |

## 変更するとき、どこを見るか

- 数字・商品・確率・ヘルプ定義などを調整したい: `catalogs/*.yaml`（型検証は `infra/catalogs.py`）または `domain/`。
- 画面・文言・Discord 操作を変えたい: `cogs/` 配下の各機能サブパッケージと `cogs/shared/`。
- DB の表や列を足したい: `infra/db/schema.py` と対応する `infra/db/*.py`。
- 新しい機能を足す: ルールと契約を `domain/`、永続化を `infra/db/`、コマンドと UI を `cogs/`、テストを `tests/` に置く。
