"""Activity のサーバー（認証・WebSocket・チンチロの卓）。Discord には、つながない。"""

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from cogs.activity.auth import AuthError, SessionSigner
from cogs.activity.server import RATE, Hub, build_app
from cogs.casino import chinchiro_engine as engine
from domain.games import chinchiro as rules
from domain.types import INITIAL_BALANCE
from tests.fakes import must
from tests.support import make_casino_deps, make_test_bot

G, C = 10, 77  # サーバーとチャンネル
CODES = {"code-1": ("tok-1", 1, "u1"), "code-2": ("tok-2", 2, "u2")}


@pytest.fixture
def bot(tmp_path):
    b = make_test_bot(str(tmp_path / "t.db"))
    yield b
    b.repos.close()


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(engine, "_TABLES", {})
    monkeypatch.setattr(engine, "_HOOKS", [])
    monkeypatch.setattr(engine, "JOIN_SECONDS", 3600)  # 締切のタスクは走らせない
    monkeypatch.setattr(engine, "ROLL_SECONDS", 3600)


async def exchange(code):
    if code not in CODES:
        raise AuthError("Discord の認証に失敗しました。開き直してください。")
    return CODES[code]


async def member_check(guild_id, user_id, channel_id):
    return f"u{user_id}" if (guild_id, channel_id) == (G, C) and user_id in (1, 2) else None


def start(bot, signer=None):
    hub = Hub(make_casino_deps(bot))
    engine.add_register_hook(hub.on_register)
    app = build_app(hub, signer or SessionSigner(), exchange, member_check)
    return hub, TestClient(TestServer(app))


async def login(client, code, guild=G, channel=C):
    r = await client.post("/api/token", json={"code": code, "guild_id": str(guild), "channel_id": str(channel)})
    return r.status, await r.json()


async def connect(client, session):
    ws = await client.ws_connect("/ws")
    await ws.send_str(json.dumps({"op": "auth", "session": session}))
    return ws


async def recv(ws, kind):
    """kind のメッセージが来るまで読む。"""
    while True:
        msg = json.loads((await ws.receive(timeout=5)).data)
        if msg["type"] == kind:
            return msg


def hand(kind, value=0):
    return [((1, 1, 1), rules.Hand(kind, value))]


# ------------------------------ セッション ------------------------------ #
def test_session_is_signed_and_expires():
    now = [1000.0]
    signer = SessionSigner(b"k" * 32, clock=lambda: now[0])
    token = signer.sign(1, G, C, "u1")
    s = signer.verify(token)
    assert s is not None and (s.user_id, s.guild_id, s.channel_id, s.name) == (1, G, C, "u1")
    body, mac = token.split(".")
    assert signer.verify(f"{body}x.{mac}") is None  # 書き換えたら通らない
    assert SessionSigner(b"j" * 32, clock=lambda: now[0]).verify(token) is None  # 別の鍵では通らない
    assert signer.verify(None) is None and signer.verify("abc") is None
    now[0] += 7 * 3600
    assert signer.verify(token) is None  # 期限切れ


# ------------------------------ 認証 ------------------------------ #
def test_token_checks_the_code_and_the_member(bot):
    async def scenario():
        _, client = start(bot)
        async with client:
            ok = await login(client, "code-1")
            bad_code = await login(client, "nope")
            other_channel = await login(client, "code-1", channel=999)
            broken = await client.post("/api/token", json={"code": "code-1"})
            return ok, bad_code, other_channel, broken.status

    ok, bad_code, other_channel, broken = asyncio.run(scenario())
    assert ok[0] == 200 and ok[1]["access_token"] == "tok-1" and ok[1]["user_id"] == "1"
    assert bad_code[0] == 401 and other_channel[0] == 403 and broken == 400


def test_websocket_needs_a_valid_session(bot):
    async def scenario():
        _, client = start(bot)
        async with client:
            ws = await connect(client, "forged.session")
            msg = json.loads((await ws.receive(timeout=5)).data)
            await ws.receive(timeout=5)
            return msg, ws.closed

    msg, closed = asyncio.run(scenario())
    assert msg["type"] == "error" and msg["fatal"] and closed


# ------------------------------ 卓 ------------------------------ #
def test_two_players_play_a_table_over_websocket(bot, monkeypatch):
    rolls = iter([hand(rules.ME, 2), hand(rules.ARASHI, 5)])
    monkeypatch.setattr(rules, "play_hand", lambda rng: next(rolls))

    async def scenario():
        _, client = start(bot)
        async with client:
            _, one = await login(client, "code-1")
            _, two = await login(client, "code-2")
            a, b = await connect(client, one["session"]), await connect(client, two["session"])
            assert (await recv(a, "hello"))["you"] == "1"
            assert (await recv(a, "state"))["table"] is None
            await recv(b, "state")
            await b.send_str(json.dumps({"op": "sit"}))
            assert (await recv(b, "error"))["text"] == "このチャンネルには、まだ卓がありません。"
            await a.send_str(json.dumps({"op": "open", "bet": 1_000}))
            assert (await recv(b, "state"))["table"]["seats"][0]["id"] == "1"
            await recv(a, "state")
            await b.send_str(json.dumps({"op": "sit"}))
            assert len((await recv(a, "state"))["table"]["seats"]) == 2
            await recv(b, "state")
            await b.send_str(json.dumps({"op": "start"}))
            assert (await recv(b, "error"))["text"] == "始められるのは、卓を立てた人だけです。"
            await a.send_str(json.dumps({"op": "start"}))
            assert (await recv(b, "state"))["table"]["phase"] == engine.ROLLING
            for ws in (a, b):
                await ws.send_str(json.dumps({"op": "roll"}))
            while (state := (await recv(a, "state"))["table"])["phase"] != engine.DONE:
                pass
            must(engine.table_at(G, C)).close()
            return state

    state = asyncio.run(scenario())
    assert [r["id"] for r in state["results"]] == ["2", "1"] and state["results"][0]["best"]
    assert state["seats"][1]["rolls"] == [{"dice": [1, 1, 1], "hand": rules.Hand(rules.ARASHI, 5).name}]
    eco = bot.repos.for_guild(G)
    assert eco.wallet.balance(2) == INITIAL_BALANCE + int(1_000 * (1 - rules.TABLE_RAKE))
    assert eco.wallet.balance(1) == INITIAL_BALANCE - 1_000


def test_open_rejects_bad_bets_and_a_second_table(bot):
    async def scenario():
        _, client = start(bot)
        async with client:
            _, one = await login(client, "code-1")
            a = await connect(client, one["session"])
            await recv(a, "state")
            texts = []
            for bet in ("1000", True, 1, 10**12):
                await a.send_str(json.dumps({"op": "open", "bet": bet}))
                texts.append((await recv(a, "error"))["text"])
            await a.send_str(json.dumps({"op": "open", "bet": 1_000}))
            await recv(a, "state")
            await a.send_str(json.dumps({"op": "open", "bet": 1_000}))
            texts.append((await recv(a, "error"))["text"])
            await a.send_str("not json")
            texts.append((await recv(a, "error"))["text"])
            must(engine.table_at(G, C)).close()
            return texts

    texts = asyncio.run(scenario())
    assert "以上の整数" in texts[0] and "以上の整数" in texts[1] and "以上の整数" in texts[2]
    assert "ペリカが足りません" in texts[3] or "残高" in texts[3]
    assert texts[4] == "このチャンネルには、まだ決着していない卓があります。"
    assert texts[5] == "操作の形が正しくありません。"


def test_too_many_messages_are_refused(bot):
    async def scenario():
        _, client = start(bot)
        async with client:
            _, one = await login(client, "code-1")
            a = await connect(client, one["session"])
            await recv(a, "state")
            for _ in range(RATE + 2):
                await a.send_str(json.dumps({"op": "stand"}))
            texts = [(await recv(a, "error"))["text"] for _ in range(RATE + 2)]
            return texts

    texts = asyncio.run(scenario())
    assert "操作が速すぎます。少し待ってください。" in texts


def test_a_table_opened_on_discord_shows_up_in_the_activity(bot):
    deps = make_casino_deps(bot)

    async def scenario():
        _, client = start(bot)
        async with client:
            _, two = await login(client, "code-2")
            b = await connect(client, two["session"])
            await recv(b, "state")
            table = engine.create(deps, G, C, 1, "u1", 1_000, 0)  # Discord の投稿から立てたのと同じ台帳に載る
            assert isinstance(table, engine.ChinchiroTable)
            await b.send_str(json.dumps({"op": "sit"}))
            state = await recv(b, "state")
            table.close()
            return state

    state = asyncio.run(scenario())
    assert [s["id"] for s in state["table"]["seats"]] == ["1", "2"]


def test_cog_does_not_start_a_server_without_the_secret(bot):
    from cogs.activity.cog import ActivityCog

    async def scenario():
        cog = ActivityCog(bot)
        await cog.cog_load()
        started = cog._runner is not None
        await cog.cog_unload()
        return started, list(engine._HOOKS)

    started, hooks = asyncio.run(scenario())
    assert not started and hooks == []
