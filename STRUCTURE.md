# ファイル・フォルダの案内

このリポジトリの、どのファイル・フォルダが、何のためのものかをまとめた一覧です。
遊び方・コマンド・ゲームのルールは [README.md](README.md) にあります。このファイルは、**コードの置き場所の地図**だけを書きます。

> ファイルを足す・名前を変えるときは、この一覧も直してください。

## 全体のつくり

層は 4 つに分かれていて、下の層ほど Discord から遠く、テストしやすくなっています。

| 層 | フォルダ | 役目 |
|---|---|---|
| コマンドの入口 | `cogs/` | スラッシュコマンドとボタンを受けて、画面（埋め込み）を出す。Discord に直接触るのは、ここと `play/` だけ |
| ゲームの進行 | `play/` | ゲームごとの、ボタン操作・演出・精算の流れ（Discord の画面） |
| ゲームのルール | `games/` | 出目・勝敗・払い戻し・還元率の計算。Discord にも DB にも触らない |
| 経済・データ | `core/` | ペリカの残高、DB の表、各機能の計算。`eco_*.py` は DB を触る部品、それ以外は計算だけの純粋なロジック |

## 最上位

| パス | 内容 |
|---|---|
| `main.py` | Bot の起動点。設定を読み、DB を開き、`cogs/` の Cog を読み込んで、Discord に接続する |
| `manage.py` | 持ち主向けの管理ツール。サーバーの端末で実行する（`grant` でペリカを付与、`servers` でサーバーの許可状況を見る）。Bot の起動中でも安全 |
| `config.example.json` | 設定ファイルの見本（トークンなどは空）。`config.json` を作るときのひな形 |
| `config.json` | 実際の設定（Bot のトークンなど）。**秘匿情報なので Git に入れない**（`.gitignore` 済み） |
| `config.json.migrated-to-server` | 古い設定ファイルの控え。`config.json.*` として Git に入れない |
| `data/` | 実行時のデータ。`casino.db`（SQLite 本体）と `bot.lock`（二重起動の防止）。**Git に入れない** |
| `README.md` | 遊び方・コマンド一覧・各ゲームと機能のルール |
| `STRUCTURE.md` | このファイル |
| `pyproject.toml` / `uv.lock` | Python のプロジェクト設定と、依存の固定（uv） |
| `requirements.txt` / `requirements-dev.txt` | 実行に必要な依存 / 開発（テスト・lint）に必要な依存 |
| `ruff.toml` | ruff（整形・lint）の設定 |
| `LICENSE` | ライセンス |
| `.gitignore` | Git に入れないものの一覧（設定・DB・ログ・キャッシュなど） |
| `tests/` | 自動テスト（下の節） |

## `cogs/` — コマンドと画面

`*_cog.py` が、Discord に読み込まれる Cog（コマンドのグループ）です。先頭が `_` のファイルは、Cog から使う部品で、単独では読み込まれません。

| ファイル | 内容 |
|---|---|
| `casino_cog.py` | `/カジノ`。遊べるゲームの入口 |
| `economy_cog.py` | `/ペリカ`。残高・デイリー・ランキング（掲示板の自動更新を含む）・送金・履歴・帝愛 |
| `shop_cog.py` | `/ショップ`。称号・カード・消耗品・スキンなどの交換 |
| `horse_cog.py` | `/競馬`。定時レースと臨時レース、馬の育成、馬券 |
| `market_cog.py` | `/証券`。株・暗号資産・債券・信用取引・オプションの売買とチャート |
| `bond_cog.py` | 債券の満期の入金・倒産抽選・破産猶予の時間切れ処理（1 分ごと） |
| `loan_cog.py` | `/貸付`。ユーザー同士の貸し借り |
| `p2p_cog.py` | `/取引`。プレイヤー間の市場と競りの画面、締切の精算（落札の告知） |
| `labor_cog.py` | `/地下 作業`・`/地下 ランキング`。クリッカー、作業員の雇用、鉱石の掘り出し |
| `quiz_cog.py` | `/クイズ`。4 択クイズと報酬 |
| `raffle_cog.py` | 抽選。参加費を預かり、締切で 1 人に全額を渡す |
| `event_cog.py` | 突発イベントの告知（1 日 1 回、30 分だけ勝ち分が増える） |
| `weekly_cog.py` | 週間ランキングの自動発表（毎週月曜 0 時）と限定称号の授与 |
| `approval_cog.py` | Bot をサーバーへ追加するとき、持ち主が DM で承認する仕組み。`/運営` コマンドもここ |
| `help_cog.py` | `/help`。カテゴリ別の案内 |
| `_admin_embeds.py` / `_admin_views.py` / `_admin_purge.py` | `/運営` の部品。埋め込みの組み立て / 確認ボタンと入力フォーム / Bot の発言の一括削除 |
| `_horse_views.py` | 競馬の画面の部品（出馬表・厩舎・馬券のボタンとモーダル） |
| `_teiai_panel.py` / `_teiai_escape.py` | `/ペリカ 帝愛` のパネル / 海外逃亡の演出 |
| `_amount.py` | 金額の入力欄に、自分の残高を見せる部品 |

## `play/` — ゲームの進行（ボタン・演出）

| ファイル | 内容 |
|---|---|
| `common.py` | 全ゲーム共通の流れ。賭け金の検証と預かり、精算、ボタンの持ち主確認、結果の表示 |
| `simple.py` | すぐ結果が出るゲーム（スロット・ルーレット） |
| `blackjack.py` | ブラックジャック（ヒット・スタンド・ダブルダウン） |
| `chinchiro.py` | チンチロ（親が先に振り、子がボタンで振る） |
| `pachinko.py` | パチンコの演出のコマ送りと、連チャンの見せ方 |

## `games/` — ゲームのルール（純粋な計算）

| ファイル | 内容 |
|---|---|
| `slot.py` | スロット。出目の重み・払い戻し・還元率の計算 |
| `roulette.py` | ルーレット。賭け方と払い戻し |
| `blackjack.py` | ブラックジャックのルール |
| `chinchiro.py` | チンチロの役と勝敗 |
| `pachinko.py` | パチンコ。当たりの確率、リーチ・予告の設計、演出の台本、還元率の計算 |
| `rtplevel.py` | 還元率のレベル（1〜6）。持ち主がゲームごとに変えられる |

## `core/` — 経済とデータ

### 中心

| ファイル | 内容 |
|---|---|
| `economy.py` | `Economy` 本体。残高・デイリー・送金・ランキング。下の `eco_*.py` を部品として混ぜ込んでいる |
| `schema.py` | DB の表の定義（作成）と、古い版への列の追加・データの移行 |
| `eco_types.py` | `Economy` と各部品が共有する定数と、結果を返す入れ物 |
| `ui.py` | 埋め込みの生成と、ペリカの表記 |
| `session.py` | 同じ人が同時に複数のゲームを始めないようにする（二重プレイ対策） |
| `confirm.py` | 実行者だけが押せる確認ボタン |
| `logthrottle.py` | 同じ原因のログを、出し続けないための間引き |

### `eco_*.py` — `Economy` に混ぜ込む、DB を触る部品

| ファイル | 内容 |
|---|---|
| `eco_labor.py` | 地下作業。クリック・疲労・作業員の決算と契約・鉱石の抽選結果の保存 |
| `eco_horse.py` | 競馬。馬・レース・出走・馬券・精算 |
| `eco_shop.py` | ショップの持ち物・装備・消耗品 |
| `eco_crypto.py` | 証券の売買・空売り・チャート |
| `eco_options.py` | オプションの購入・満期の決済・売却 |
| `eco_p2p.py` | プレイヤー間の取引（定価の市場と競りの、出品・買う・入札・取り消し・締切の精算） |
| `p2p.py` | 取引の数字と決まり（値段の幅・手数料・入札幅・延長）。純粋なロジック |
| `eco_bonds.py` | 債券の購入・満期・倒産抽選・売却 |
| `eco_debt.py` | 帝愛の借金・強制執行・種銭・身分・自己破産 |
| `eco_bank.py` | 帝愛銀行（預金・準備率）、破産猶予、CDS |
| `eco_auction.py` | 破産で差し押さえた品の競売 |
| `eco_loans.py` | ユーザー同士の貸し借りの記録 |
| `eco_quiz.py` | クイズの問題・回答・報酬の支払い |
| `eco_raffle.py` | 抽選の参加費の預かりと、当選・返金 |
| `eco_events.py` | 突発イベントと特別イベントの進み具合 |
| `eco_market_events.py` | 市場イベント（ボラティリティ予告）の記録と抽選 |
| `eco_records.py` | 連勝・デイリーミッション・週間ランキング・ゲーム損益の記録 |
| `eco_pachinko.py` | パチンコの実際の結果の記録（設計した数字との比較用） |
| `eco_rtp.py` | ゲームごとの還元率レベルの保存 |
| `eco_guild.py` | サーバーの承認・ランキング掲示・ニックネーム設定 |

### 純粋なロジック（DB にも Discord にも触らない）

| ファイル | 内容 |
|---|---|
| `labor.py` | 地下作業の数字（工具・疲労・作業員・契約料）と、鉱石の表・抽選・契約の決算の計算 |
| `horse.py` | 競馬の数字とレースのシミュレーション |
| `market.py` / `market_events.py` | 証券の価格の動き・売買の計算・文字のチャート / ボラティリティ予告と相関崩壊の警報 |
| `options.py` | オプションの値付け |
| `bonds.py` | 債券の種類・利率・期間 |
| `teiai.py` | 帝愛ファイナンス（借金・身分・銀行）の数字 |
| `loans.py` | 貸し借りの利息計算（日割りの単利） |
| `shop.py` | ショップの商品一覧（カタログ） |
| `skins.py` | ゲーム演出の変更品（スキン）の見た目 |
| `missions.py` | デイリーミッションと、連勝・大勝ちの演出 |
| `events.py` | 突発イベントの抽選と、勝ち分の増加 |
| `weekly.py` | 週間ランキングの週の数え方と、発表の文面 |
| `ranking.py` | ランキングの表示（コマンドと掲示板で共通） |
| `history.py` | ペリカの増減の履歴を、読みやすい行にまとめる |
| `quizimport.py` / `quizpay.py` / `duration.py` | クイズの一括入力の読み取り / ユーザー出題の報酬の数字 / 「2時間」などの期間の読み取り |
| `approval.py` | Bot を追加できるサーバーの許可リストと、持ち主の承認 |
| `nick.py` | 称号をニックネームの後ろに付け外しする、文字列処理 |

## `tests/` — 自動テスト

`pytest` で実行します（`.venv/bin/python -m pytest -q`）。ファイル名は、対象の機能に合わせています（例: `test_labor.py` は地下作業、`test_pachinko_model.py` はパチンコの設計と演出）。

| ファイル | 内容 |
|---|---|
| `conftest.py` | テスト共通の準備（記録をサーバーごとに分けず、1 つの口座として確かめる設定など） |
| `test_flows.py` | コマンドの一覧・画面の流れ全体の確認 |
| `test_economy.py` / `test_guild_scope.py` | 残高・送金・サーバーごとの口座の分離 |
| `test_games.py` / `test_rtplevel.py` / `test_pachinko_model.py` | ゲームのルール・還元率・パチンコの設計と演出 |
| `test_labor.py` | 地下作業（疲労・雇用・契約・鉱石・旧契約の返金） |
| `test_horse.py` / `test_horse_cog.py` | 競馬のシミュレーションと画面 |
| `test_market.py` / `test_market_events.py` / `test_options.py` / `test_bonds.py` / `test_margin.py` / `test_symbols.py` / `test_pair_and_write.py` | 証券・債券・オプション・信用取引 |
| `test_teiai.py` / `test_bank.py` / `test_escape_ui.py` | 帝愛・銀行・海外逃亡 |
| `test_shop.py` / `test_shop_panel.py` / `test_skins_collection.py` / `test_nick.py` | ショップ・スキン・称号 |
| `test_loans.py` / `test_quiz*.py` / `test_raffle.py` / `test_events.py` / `test_missions.py` / `test_weekly.py` / `test_leaderboard.py` / `test_history_stats.py` | 貸付・クイズ・抽選・イベント・ミッション・ランキング・履歴 |
| `test_approval.py` / `test_purge.py` / `test_channel_target.py` / `test_public_results.py` / `test_amount.py` / `test_autoplay_retired.py` | 承認・発言削除・投稿先の指定・結果の公開・金額入力・廃止した機能の確認 |

## 変更するとき、どこを見るか

- 数字を調整したい（稼ぎ・確率・値段）: `core/*.py` の純粋なロジック（`labor.py`・`teiai.py`・`bonds.py` など）か、`games/`。
- 画面・文言を変えたい: `cogs/*_cog.py` と `play/`。
- DB の表や列を足したい: `core/schema.py`（表の定義と、古い版への列の追加・移行）と、対応する `core/eco_*.py`。
- 新しい機能を足す: ロジックを `core/`（または `games/`）、DB を触る部分を `core/eco_*.py`、コマンドを `cogs/`、テストを `tests/` に置く。
