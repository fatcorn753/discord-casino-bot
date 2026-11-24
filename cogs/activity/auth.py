"""Activity の画面の認証。Discord の OAuth2 の code をトークンに替え、本人を確かめて、Bot のセッションを渡す。

流れ（Discord の Embedded App SDK の手順）:
1. 画面が SDK の authorize で code をもらい、`POST /api/token` に code・サーバー・チャンネルを送る
2. ここで code を access token に替え（client secret はサーバーにだけ置く）、`/users/@me` で本人を確かめる
3. Bot が、その人がサーバーのメンバーで、そのチャンネルを見られるかを確かめる
4. access token（画面が SDK の authenticate に使う）と、Bot のセッション（WebSocket で名乗るのに使う）を返す

セッションは HMAC で署名した短い文字列で、だれが・どのサーバーの・どのチャンネルで遊んでいるかだけを持つ。
署名の鍵は起動のたびに作り直すので、Bot が再起動したら、画面は認証からやり直す。
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp

DISCORD_API = "https://discord.com/api/v10"
SESSION_SECONDS = 6 * 3600


@dataclass(frozen=True)
class Session:
    """Activity の画面で遊んでいる人。"""

    user_id: int
    guild_id: int
    channel_id: int
    name: str
    expires: float


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionSigner:
    """セッションに署名し、確かめる。"""

    def __init__(self, key: bytes | None = None, clock: Callable[[], float] = time.time):
        self._key = key or secrets.token_bytes(32)
        self._clock = clock

    def _mac(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    def sign(self, user_id: int, guild_id: int, channel_id: int, name: str) -> str:
        payload = {"u": user_id, "g": guild_id, "c": channel_id, "n": name, "e": self._clock() + SESSION_SECONDS}
        body = _b64(json.dumps(payload, ensure_ascii=False).encode())
        return f"{body}.{self._mac(body)}"

    def verify(self, token: object) -> Session | None:
        """正しく署名され、期限内のセッションなら Session。それ以外は None。"""
        if not isinstance(token, str) or token.count(".") != 1:
            return None
        body, mac = token.split(".")
        if not hmac.compare_digest(mac, self._mac(body)):
            return None
        try:
            p = json.loads(_unb64(body))
            s = Session(int(p["u"]), int(p["g"]), int(p["c"]), str(p["n"]), float(p["e"]))
        except (ValueError, KeyError, TypeError):
            return None
        return s if s.expires > self._clock() else None


class AuthError(Exception):
    """認証に失敗した（画面には理由の文だけを返す）。"""


# code を (access token, ユーザーの ID, 表示名) に替える関数。テストでは偽物に差し替える
Exchanger = Callable[[str], Awaitable[tuple[str, int, str]]]


def discord_exchanger(client_id: str, client_secret: str, session: aiohttp.ClientSession) -> Exchanger:
    """Discord の OAuth2 で code を access token に替え、`/users/@me` で本人を確かめる。"""

    async def exchange(code: str) -> tuple[str, int, str]:
        form = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
        }
        async with session.post(f"{DISCORD_API}/oauth2/token", data=form) as r:
            if r.status != 200:
                raise AuthError("Discord の認証に失敗しました。開き直してください。")
            token = (await r.json()).get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthError("Discord の認証に失敗しました。開き直してください。")
        async with session.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status != 200:
                raise AuthError("Discord のユーザーを確かめられませんでした。")
            me = await r.json()
        return token, int(me["id"]), str(me.get("global_name") or me.get("username") or "")

    return exchange
