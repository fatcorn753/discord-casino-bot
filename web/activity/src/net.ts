// Discord の中で開いたときの認証と WebSocket。手順は docs/activity.md。

import { DiscordSDK } from "@discord/embedded-app-sdk";
import type { Link, Op, ServerMessage } from "./types";

class SocketLink implements Link {
  private listeners: ((msg: ServerMessage) => void)[] = [];
  private ws: WebSocket | null = null;
  private retry = 0;
  private stopped = false;

  constructor(private session: string) {
    this.open();
  }

  private emit(msg: ServerMessage) {
    for (const l of this.listeners) l(msg);
  }

  private open() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/ws`);
    this.ws = ws;
    ws.addEventListener("open", () => {
      this.retry = 0;
      ws.send(JSON.stringify({ op: "auth", session: this.session }));
    });
    ws.addEventListener("message", (ev) => {
      let msg: ServerMessage;
      try {
        msg = JSON.parse(String(ev.data)) as ServerMessage;
      } catch {
        return;
      }
      if (msg.type === "error" && msg.fatal) this.stopped = true;
      this.emit(msg);
    });
    ws.addEventListener("close", () => {
      if (this.stopped) return;
      // 切れたら、間をあけてつなぎ直す（同じセッションのまま。期限切れなら fatal が届く）
      const wait = Math.min(8000, 500 * 2 ** this.retry++);
      this.emit({ type: "error", text: "接続が切れました。つなぎ直しています。" });
      setTimeout(() => this.open(), wait);
    });
  }

  send(op: Op) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(op));
    else this.emit({ type: "error", text: "まだつながっていません。少し待ってから、もう一度押してください。" });
  }

  onMessage(listener: (msg: ServerMessage) => void) {
    this.listeners.push(listener);
  }
}

/** Discord の SDK で本人を確かめ、Bot のセッションをもらって WebSocket をつなぐ。 */
export async function connectDiscord(): Promise<Link> {
  const config = await fetch("/api/config").then((r) => r.json() as Promise<{ client_id: string }>);
  const sdk = new DiscordSDK(config.client_id);
  await sdk.ready();
  if (!sdk.guildId || !sdk.channelId) throw new Error("サーバーのチャンネルの中で開いてください。");
  const { code } = await sdk.commands.authorize({
    client_id: config.client_id,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify"],
  });
  const res = await fetch("/api/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, guild_id: sdk.guildId, channel_id: sdk.channelId }),
  });
  const body = (await res.json()) as { access_token?: string; session?: string; error?: string };
  if (!res.ok || !body.access_token || !body.session) throw new Error(body.error ?? "認証に失敗しました。");
  await sdk.commands.authenticate({ access_token: body.access_token });
  return new SocketLink(body.session);
}
