# Activity（Discord の中で開く画面）

チンチロの卓を、Discord の Activity（Discord のアプリの中で開く Web の画面）でも遊べるようにします。今の `/カジノ チンチロ卓` の投稿とボタンはそのまま残り、同じチャンネルなら、投稿と Activity の画面は同じ卓を見せます。

## 仕組み

- Bot と同じプロセスで、aiohttp の HTTP サーバーが動きます（`cogs/activity/`）。既定は `127.0.0.1:8787` です。
- 外からは、Tailscale Funnel などの HTTPS で、このサーバーに届けます。Discord の Developer Portal の URL Mapping に、その HTTPS のアドレスを登録します。
- 認証:
  1. 画面が Discord の SDK で code をもらい、`POST /api/token` に送ります。
  2. サーバーが、client secret を使って code を access token に替え、`/users/@me` で本人を確かめます。
  3. Bot が、その人がサーバーのメンバーで、そのチャンネルを見られるかを確かめます。
  4. 画面に、access token と Bot のセッション（署名つき。6時間で切れる）を返します。
- `GET /ws` の WebSocket で、最初にセッションを名乗ると、そのチャンネルの卓の様子が届きます。画面から送れるのは、操作（卓を立てる・座る・立つ・始める・振る）だけです。出目とお金の処理は、すべてサーバーで行います。
- 卓の進め方は `cogs/casino/chinchiro_engine.py` にあり、Discord の投稿（`cogs/casino/chinchiro_table.py`）と Activity の画面の両方が、同じ卓を見ます。

## 設定

`config.json` に `activity` の項目があるときだけ、サーバーを立てます。なければ、今までどおり Bot だけが動きます。

```json
"activity": {
  "client_secret": "（Developer Portal の OAuth2 の Client Secret）",
  "host": "127.0.0.1",
  "port": 8787
}
```

- `client_id` は省略できます（Bot のアプリケーション ID を使います）。
- client secret は `config.json` にだけ置き、コード・ログ・コミットには入れません。

## 守っていること

- サーバーは 127.0.0.1 でだけ待ち受けます。公開するのは、Funnel で届けるこのポートだけです。
- 画面から金額や本人を偽れないよう、本人はセッションで決め、賭け金は今のゲームと同じ検証（残高・最低額・機能の停止）を通します。
- 1通の大きさ（4 KB）と、1秒あたりの操作の数（8回）に上限があります。
- アクセスの記録は残しません。

## 画面（`web/activity/`）

TypeScript と Vite で作り、Canvas のドット絵と Web Audio の合成音で卓を見せます。絵と音は、このリポジトリで描いたものだけです。

- 開発: `npm --prefix web/activity install` のあと `npm --prefix web/activity run dev`。`http://localhost:5173/?mock` で、Discord と Bot なしに試し遊びできます（偽の相手が座って振ります。`?mock` は開発中だけ効き、ビルドには入りません）。
- ビルド: `npm --prefix web/activity run build`。型の検査のあと、`web/activity/dist/` に書き出します。Bot はこの `dist/` を配ります。
- 本番へ送るとき: Mac でビルドしてから送ります。`web/activity/node_modules` は送らず、`web/activity/dist` は送ります（`dist` は git に入れていません）。

## まだないもの

- Activity から立てた卓は、Discord のチャンネルには投稿しません。
