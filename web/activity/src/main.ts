// チンチロの卓の画面。卓の様子をサーバーから受け取り、絵（scene.ts）と一覧・ボタンを描き直す。

import "./design/tokens.css";
import "./design/base.css";
import "./design/components.css";
import "./app.css";

import { connectDiscord } from "./net";
import { Scene, type Throw } from "./scene";
import * as sfx from "./sfx";
import type { Link, Op, Seat, ServerMessage, Table } from "./types";

const MIN_BET = 100;
const ICON_ERROR = '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/>';

const $ = <T extends HTMLElement>(sel: string) => document.querySelector(sel) as T;
const yen = (n: number) => `${n.toLocaleString("ja-JP")}ペリカ`;
const signed = (n: number) => (n > 0 ? `+${n.toLocaleString("ja-JP")}` : n.toLocaleString("ja-JP"));

function icon(paths: string): string {
  return `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

const PHASE_LABEL: Record<Table["phase"], string> = {
  lobby: "受付中",
  rolling: "勝負中",
  done: "決着",
  folded: "たたみました",
  failed: "精算の失敗",
};

class App {
  private me = "";
  private table: Table | null = null;
  private seen = new Map<string, number>(); // 席ごとに、絵に渡した振りの数
  private revealed = new Map<string, string>(); // 席ごとに、転がし終えて見せた役
  private first = true;
  private scene: Scene;
  private celebrated = false;
  private timer = 0;

  constructor(private link: Link) {
    this.scene = new Scene($<HTMLCanvasElement>("#scene"));
    this.scene.onLabel = (t) => {
      this.showLabel(t);
      if (t) {
        this.revealed.set(t.seat, t.label);
        this.render();
      }
    };
    this.scene.onIdle = () => this.render();
    link.onMessage((m) => this.receive(m));
    $("#actions").addEventListener("click", (ev) => this.click(ev));
    $("#open-form").addEventListener("submit", (ev) => {
      ev.preventDefault();
      this.open();
    });
    $("#sound").addEventListener("click", () => {
      sfx.setSound(!sfx.soundOn());
      this.renderSound();
    });
    document.addEventListener("pointerdown", () => sfx.unlock(), { once: false });
    this.renderSound();
    this.timer = window.setInterval(() => this.renderClock(), 1000);
  }

  private receive(m: ServerMessage) {
    if (m.type === "hello") {
      this.me = m.you;
      return;
    }
    if (m.type === "error") {
      this.showError(m.text);
      if (m.fatal) window.clearInterval(this.timer);
      return;
    }
    this.clearError();
    const prev = this.table;
    this.table = m.table;
    if (!m.table || (prev && prev.closes_at !== m.table.closes_at)) {
      // 新しい卓になった
      this.seen.clear();
      this.revealed.clear();
      this.celebrated = false;
      if (m.table) this.scene.show(null);
    }
    this.queueThrows(m.table);
    this.first = false;
    this.render();
  }

  /** まだ見せていない振りを、絵に転がしてもらう。開いたときにもう振ってあった分は、動かさずに置く。 */
  private queueThrows(table: Table | null) {
    if (!table) return;
    const throws: Throw[] = [];
    let last: number[] | null = null;
    for (const s of table.seats) {
      const shown = this.seen.get(s.id) ?? 0;
      s.rolls.slice(shown).forEach((r) => throws.push({ dice: r.dice, label: r.hand, who: s.name, seat: s.id }));
      if (s.rolls.length) {
        last = s.rolls[s.rolls.length - 1].dice;
        if (this.first) this.revealed.set(s.id, s.rolls[s.rolls.length - 1].hand);
      }
      this.seen.set(s.id, s.rolls.length);
    }
    if (this.first) this.scene.show(last);
    else if (throws.length) this.scene.play(throws);
  }

  private send(op: Op) {
    sfx.unlock();
    this.clearError();
    this.link.send(op);
  }

  private open() {
    const input = $<HTMLInputElement>("#bet");
    const bet = Number(input.value);
    if (!Number.isInteger(bet) || bet < MIN_BET) {
      this.fieldError(`賭け金は ${MIN_BET.toLocaleString("ja-JP")}ペリカ 以上の整数で入れてください。`);
      input.focus();
      return;
    }
    this.fieldError("");
    this.send({ op: "open", bet });
  }

  private click(ev: Event) {
    const btn = (ev.target as HTMLElement).closest<HTMLButtonElement>("button[data-op]");
    if (!btn) return;
    this.send({ op: btn.dataset.op as "sit" | "stand" | "start" | "roll" });
  }

  // ------------------------------ 表示 ------------------------------ //
  private showLabel(t: Throw | null) {
    const el = $("#label");
    if (!t) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    el.innerHTML = `<span class="who">${esc(t.who)}</span><span class="hand">${esc(t.label)}</span>`;
  }

  private showError(text: string) {
    const el = $("#error");
    el.hidden = false;
    el.innerHTML = `${icon(ICON_ERROR)}<p>${esc(text)}</p>`;
  }

  private clearError() {
    $("#error").hidden = true;
  }

  private fieldError(text: string) {
    const el = $("#bet-error");
    el.hidden = !text;
    el.innerHTML = text ? `${icon(ICON_ERROR)}${esc(text)}` : "";
    $("#bet").setAttribute("aria-invalid", text ? "true" : "false");
  }

  private renderSound() {
    const b = $("#sound");
    b.textContent = sfx.soundOn() ? "音あり" : "音なし";
    b.setAttribute("aria-pressed", String(sfx.soundOn()));
  }

  private renderClock() {
    const t = this.table;
    const el = $("#clock");
    if (!t || t.phase !== "lobby") {
      el.textContent = t?.phase === "rolling" ? `${t.roll_seconds}秒で、まだの人は自動で振ります` : "";
      return;
    }
    const left = Math.max(0, Math.round(t.closes_at - Date.now() / 1000));
    el.textContent = `締切まで ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  }

  private seatRow(s: Seat, t: Table): string {
    const done = t.phase === "done" && this.scene.idle;
    const r = done ? t.results.find((x) => x.id === s.id) : undefined;
    const shown = this.seen.get(s.id) ?? 0;
    const last = this.revealed.get(s.id) ?? (shown ? "振っています" : "");
    const tags = [s.id === t.host ? '<span class="td-badge">親</span>' : "", s.id === this.me ? '<span class="td-badge" data-tone="accent">あなた</span>' : ""].join("");
    const net = r
      ? `<span class="net num" data-sign="${Math.sign(r.net)}">${signed(r.net)}</span>`
      : `<span class="hand-now">${esc(last || (t.phase === "rolling" ? "まだ振っていません" : ""))}</span>`;
    return `<li class="seat${r?.best ? " best" : ""}"><span class="name">${esc(s.name)}</span>${tags}${r?.best ? '<span class="td-badge" data-tone="success">総取り</span>' : ""}${net}</li>`;
  }

  render() {
    const t = this.table;
    this.renderClock();
    $("#title-bet").textContent = t ? `賭け金 ${yen(t.bet)}（全員同じ）` : "";
    $("#phase").textContent = t ? PHASE_LABEL[t.phase] : "卓なし";
    $("#seats").innerHTML = t ? t.seats.map((s) => this.seatRow(s, t)).join("") : "";
    $("#rule").textContent = t
      ? `いちばん強い役の人が総取りします（並んだら山分け）。負けた分の ${Math.round(t.rake * 100)}% は場代です。${t.min_seats}〜${t.max_seats}人で遊べます。`
      : "";
    const settled = !t || ["done", "folded", "failed"].includes(t.phase);
    $("#open-form").hidden = !settled || !this.scene.idle;
    $("#empty").hidden = Boolean(t);
    if (t?.phase === "done" && this.scene.idle && !this.celebrated) {
      this.celebrated = true;
      const mine = t.results.find((r) => r.id === this.me);
      if (mine?.best && mine.net > 0) {
        this.scene.celebrate();
        sfx.win();
      } else if (mine && mine.net < 0) sfx.lose();
    }
    $("#notice").textContent =
      t?.phase === "folded" ? "人数がそろわなかったので、卓をたたみました。賭け金は全額返しました。" : t?.phase === "failed" ? "精算に失敗しました。精算前の賭け金は、全額返しました。" : "";
    // 変わったときだけ書き換える（押そうとしたボタンや、キーボードのフォーカスを消さないため）
    const buttons = this.buttons(t);
    if ($("#actions").innerHTML !== buttons) $("#actions").innerHTML = buttons;
  }

  private buttons(t: Table | null): string {
    if (!t) return "";
    const seated = t.seats.some((s) => s.id === this.me);
    const b = (op: string, label: string, primary = false) =>
      `<button class="td-btn" type="button" data-op="${op}"${primary ? ' data-variant="primary"' : ""}>${label}</button>`;
    if (t.phase === "lobby") {
      if (!seated) return b("sit", `${yen(t.bet)} を出して座る`, true);
      return b("stand", "席を立つ") + (t.host === this.me ? b("start", "始める", true) : "");
    }
    if (t.phase === "rolling" && seated) {
      const mine = t.seats.find((s) => s.id === this.me);
      if (mine && !mine.rolls.length) return b("roll", "振る", true);
    }
    return "";
  }
}

async function boot() {
  let link: Link;
  try {
    if (import.meta.env.DEV && new URLSearchParams(location.search).has("mock")) {
      const { createMock } = await import("./mock");
      link = createMock();
    } else {
      link = await connectDiscord();
    }
  } catch (err) {
    $("#error").hidden = false;
    $("#error").innerHTML = `${icon(ICON_ERROR)}<p>${esc(err instanceof Error ? err.message : "開けませんでした。")} Discord で開き直してください。</p>`;
    return;
  }
  new App(link);
}

void boot();
