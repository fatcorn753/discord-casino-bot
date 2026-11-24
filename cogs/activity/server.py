"""Activity の画面のための HTTP サーバー（aiohttp。Bot と同じプロセスで動く）。

- `GET /api/config`: 画面が Discord の SDK を始めるための client_id
- `POST /api/token`: 認証（cogs/activity/auth.py）。成功したら access token と Bot のセッションを返す
- `GET /ws`: WebSocket。最初のメッセージでセッションを名乗り、そのチャンネルのチンチロの卓の様子を受け取る。
  画面から送れるのは操作（卓を立てる・座る・立つ・始める・振る）だけで、出目とお金の処理は、すべてここで決める
- それ以外: 画面のファイル（web/activity/dist）

卓の進め方は cogs/casino/chinchiro_engine.py。Discord の投稿から立てた卓も、同じチャンネルなら同じ卓として見える。
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from cogs.activity.auth import AuthError, Exchanger, Session, SessionSigner
from cogs.casino import chinchiro_engine as engine
from cogs.casino.chinchiro_engine import ChinchiroTable
from cogs.casino.common import BeginFailure, CasinoDeps
from domain.games import chinchiro as rules
from domain.games.rtplevel import MIN_BET

_log = logging.getLogger(__name__)

# (サーバー, ユーザー, チャンネル) で、その人がそのチャンネルにいられるかと、表示名を返す（いられなければ None）
MemberCheck = Callable[[int, int, int], Awaitable[str | None]]

MAX_MESSAGE = 4096  # 画面から受け取る1通の上限（操作だけなので小さい）
RATE = 8  # 1秒に受け付ける操作の数（押しっぱなし・いたずら対策）
AUTH_SECONDS = 10  # 接続してから名乗るまでの猶予


def table_state(table: ChinchiroTable | None) -> dict[str, Any] | None:
    """卓の様子を、画面に送る形にする。ID は JavaScript で桁が落ちないよう文字列にする。"""
    if table is None:
        return None
    return {
        "phase": table.phase,
        "bet": table.bet,
        "host": str(table.host_id),
        "closes_at": table.closes_at,
        "roll_seconds": engine.ROLL_SECONDS,
        "max_seats": engine.MAX_SEATS,
        "min_seats": engine.MIN_SEATS,
        "rake": rules.TABLE_RAKE,
        "seats": [
            {
                "id": str(uid),
                "name": table.name_of(uid),
                "rolls": [{"dice": list(d), "hand": h.name} for d, h in table.rolls.get(uid, [])],
            }
            for uid in table.seats
        ],
        "results": [{"id": str(r.user_id), "name": r.name, "net": r.net, "best": r.best} for r in table.results],
    }


class Client:
    """WebSocket でつないでいる画面1つ。"""

    def __init__(self, ws: web.WebSocketResponse, session: Session):
        self.ws = ws
        self.session = session
        self._window = 0.0
        self._count = 0

    @property
    def room(self) -> tuple[int, int]:
        return self.session.guild_id, self.session.channel_id

    def allow(self, now: float) -> bool:
        """1秒あたりの操作の数を数える。多すぎたら False。"""
        if now - self._window >= 1:
            self._window, self._count = now, 0
        self._count += 1
        return self._count <= RATE

    async def send(self, data: dict[str, Any]) -> None:
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(data, ensure_ascii=False))


class Hub:
    """チャンネルごとに、つないでいる画面を持ち、卓が変わったら全員に送る。"""

    def __init__(self, deps: CasinoDeps, clock: Callable[[], float] = time.time):
        self.deps = deps
        self._clock = clock
        self.rooms: dict[tuple[int, int], set[Client]] = {}

    # 卓が台帳に載ったら、その卓の変化を受け取る
    def on_register(self, table: ChinchiroTable) -> None:
        table.listeners.append(self._on_change)

    async def _on_change(self, table: ChinchiroTable) -> None:
        await self.broadcast((table.guild_id, table.channel_id))

    async def broadcast(self, room: tuple[int, int]) -> None:
        state = {"type": "state", "table": table_state(engine.table_at(*room))}
        for c in list(self.rooms.get(room, ())):
            try:
                await c.send(state)
            except (ConnectionError, RuntimeError):  # 切れた画面は、leave で外れる
                pass

    def join(self, client: Client) -> None:
        self.rooms.setdefault(client.room, set()).add(client)

    def leave(self, client: Client) -> None:
        room = self.rooms.get(client.room)
        if room is not None:
            room.discard(client)
            if not room:
                del self.rooms[client.room]

    async def handle(self, client: Client, data: object) -> str | None:
        """画面からの操作を1つ処理する。断るときは理由の文を返す。"""
        if not isinstance(data, dict):
            return "操作の形が正しくありません。"
        op = data.get("op")
        s = client.session
        table = engine.table_at(s.guild_id, s.channel_id)
        if op == "open":
            bet = data.get("bet")
            if not isinstance(bet, int) or isinstance(bet, bool) or bet < MIN_BET:
                return f"賭け金は {MIN_BET:,}ペリカ 以上の整数で指定してください。"
            got = engine.create(self.deps, s.guild_id, s.channel_id, s.user_id, s.name, bet, self._clock())
            if isinstance(got, BeginFailure):
                return got.text
            await self.broadcast(client.room)
            return None
        if table is None:
            return "このチャンネルには、まだ卓がありません。"
        if op == "sit":
            reason = await table.sit(s.user_id, s.name)
            return reason.text if isinstance(reason, BeginFailure) else reason
        if op == "stand":
            return None if await table.stand(s.user_id) else "座っていないか、もう始まっています。"
        if op == "start":
            if s.user_id != table.host_id:
                return "始められるのは、卓を立てた人だけです。"
            await table.play()
            return None
        if op == "roll":
            return await table.roll(s.user_id)
        return "知らない操作です。"


def build_app(
    hub: Hub,
    signer: SessionSigner,
    exchange: Exchanger,
    member_check: MemberCheck,
    static_dir: Path | None = None,
    client_id: str = "",
) -> web.Application:
    app = web.Application(client_max_size=MAX_MESSAGE)

    async def config(request: web.Request) -> web.Response:
        # 画面が Discord の SDK を始めるのに使う（アプリケーション ID は公開の値）
        return web.json_response({"client_id": client_id})

    async def token(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            code, guild_id, channel_id = body["code"], int(body["guild_id"]), int(body["channel_id"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "送られた内容が正しくありません。"}, status=400)
        if not isinstance(code, str) or not code:
            return web.json_response({"error": "送られた内容が正しくありません。"}, status=400)
        try:
            access_token, user_id, name = await exchange(code)
        except AuthError as exc:
            return web.json_response({"error": str(exc)}, status=401)
        except Exception:
            _log.exception("Activity の認証に失敗")
            return web.json_response({"error": "認証に失敗しました。時間をおいて開き直してください。"}, status=502)
        display = await member_check(guild_id, user_id, channel_id)
        if display is None:
            return web.json_response({"error": "このサーバーのこのチャンネルでは遊べません。"}, status=403)
        session = signer.sign(user_id, guild_id, channel_id, display or name)
        return web.json_response({"access_token": access_token, "session": session, "user_id": str(user_id)})

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_MESSAGE)
        await ws.prepare(request)
        try:
            first = await ws.receive(timeout=AUTH_SECONDS)
            data = json.loads(first.data) if first.type == WSMsgType.TEXT else None
        except (TimeoutError, ValueError):
            data = None
        session = signer.verify(data.get("session")) if isinstance(data, dict) and data.get("op") == "auth" else None
        if session is None:
            await ws.send_str(json.dumps({"type": "error", "text": "認証からやり直してください。", "fatal": True}))
            await ws.close()
            return ws
        client = Client(ws, session)
        hub.join(client)
        try:
            await client.send({"type": "hello", "you": str(session.user_id), "name": session.name})
            await client.send({"type": "state", "table": table_state(engine.table_at(*client.room))})
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                if not client.allow(time.monotonic()):
                    await client.send({"type": "error", "text": "操作が速すぎます。少し待ってください。"})
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    data = None
                reason = await hub.handle(client, data)
                if reason:
                    await client.send({"type": "error", "text": reason})
        finally:
            hub.leave(client)
        return ws

    app.router.add_get("/api/config", config)
    app.router.add_post("/api/token", token)
    app.router.add_get("/ws", ws_handler)
    if static_dir is not None and (static_dir / "index.html").is_file():
        index = static_dir / "index.html"

        async def home(request: web.Request) -> web.FileResponse:
            return web.FileResponse(index)

        app.router.add_get("/", home)
        app.router.add_static("/", static_dir, show_index=False)
    return app
