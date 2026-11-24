// 開発用の模擬サーバー（`npm run dev` で開き、URL に ?mock を付けたときだけ使う。本番のビルドには入らない）。
// 見た目と動きを Discord の外で確かめるためのもの。役の強さとお金の計算は本物（サーバー）とは別の簡易版。

import type { Link, Op, Roll, ServerMessage, Table } from "./types";

const ME = "1";
const FRIENDS = [
  { id: "2", name: "ささき" },
  { id: "3", name: "とても長い名前のプレイヤーさん（テスト用）" },
];

function die() {
  return 1 + Math.floor(Math.random() * 6);
}

function judge(d: number[]): { name: string; strength: number } | null {
  const s = [...d].sort();
  if (s[0] === s[2]) return { name: s[0] === 1 ? "ピンゾロ" : `${s[0]}のアラシ`, strength: 100 + s[0] };
  if (s.join() === "4,5,6") return { name: "シゴロ", strength: 90 };
  if (s.join() === "1,2,3") return { name: "ヒフミ", strength: -10 };
  if (s[0] === s[1]) return { name: `${s[2]}の目`, strength: s[2] };
  if (s[1] === s[2]) return { name: `${s[0]}の目`, strength: s[0] };
  return null;
}

function hand(): Roll[] {
  const rolls: Roll[] = [];
  for (let i = 0; i < 3; i++) {
    const dice = [die(), die(), die()];
    const j = judge(dice);
    rolls.push({ dice, hand: j ? j.name : "目なし" });
    if (j) break;
  }
  return rolls;
}

function strength(rolls: Roll[]): number {
  const j = judge(rolls[rolls.length - 1].dice);
  return j ? j.strength : 0;
}

export function createMock(): Link {
  const listeners: ((m: ServerMessage) => void)[] = [];
  let table: Table | null = null;
  const emit = (m: ServerMessage) => setTimeout(() => listeners.forEach((l) => l(m)), 120);
  const push = () => emit({ type: "state", table: table && structuredClone(table) });
  const fail = (text: string) => emit({ type: "error", text });

  function finish() {
    if (!table) return;
    for (const s of table.seats) if (!s.rolls.length) s.rolls = hand();
    const best = Math.max(...table.seats.map((s) => strength(s.rolls)));
    const winners = table.seats.filter((s) => strength(s.rolls) === best);
    const losers = table.seats.length - winners.length;
    const share = Math.floor((table.bet * losers * (1 - table.rake)) / winners.length);
    table.results = table.seats
      .map((s) => ({ id: s.id, name: s.name, best: winners.includes(s), net: winners.includes(s) ? share : -table!.bet }))
      .sort((a, b) => Number(b.best) - Number(a.best));
    if (winners.length === table.seats.length) table.results.forEach((r) => (r.net = 0));
    table.phase = "done";
    push();
  }

  function handle(op: Op) {
    if (op.op === "open") {
      if (table && !["done", "folded", "failed"].includes(table.phase))
        return fail("このチャンネルには、まだ決着していない卓があります。");
      table = {
        phase: "lobby",
        bet: op.bet,
        host: ME,
        closes_at: Date.now() / 1000 + 60,
        roll_seconds: 45,
        max_seats: 6,
        min_seats: 2,
        rake: 0.1,
        seats: [{ id: ME, name: "あなた", rolls: [] }],
        results: [],
      };
      push();
      // 友達が少しずつ座ってくる
      FRIENDS.forEach((f, i) =>
        setTimeout(() => {
          if (table?.phase === "lobby") {
            table.seats.push({ ...f, rolls: [] });
            push();
          }
        }, 1500 * (i + 1)),
      );
      return;
    }
    if (!table) return fail("このチャンネルには、まだ卓がありません。");
    const mine = table.seats.find((s) => s.id === ME);
    if (op.op === "sit") return fail(mine ? "もう座っています。" : "もう始まっています。");
    if (op.op === "stand") {
      if (table.phase !== "lobby" || !mine) return fail("座っていないか、もう始まっています。");
      table.seats = table.seats.filter((s) => s.id !== ME);
      return push();
    }
    if (op.op === "start") {
      if (table.phase !== "lobby") return;
      table.phase = table.seats.length < table.min_seats ? "folded" : "rolling";
      push();
      if (table.phase === "rolling")
        table.seats
          .filter((s) => s.id !== ME)
          .forEach((s, i) =>
            setTimeout(() => {
              if (table?.phase !== "rolling") return;
              s.rolls = hand();
              if (table.seats.every((x) => x.rolls.length)) finish();
              else push();
            }, 2500 * (i + 1)),
          );
      return;
    }
    if (op.op === "roll") {
      if (!mine) return fail("この卓に座っていません。");
      if (table.phase !== "rolling") return fail("まだ始まっていません。");
      if (mine.rolls.length) return fail("もう振りました。");
      mine.rolls = hand();
      if (table.seats.every((x) => x.rolls.length)) finish();
      else push();
    }
  }

  emit({ type: "hello", you: ME, name: "あなた" });
  push();
  return {
    send: handle,
    onMessage: (l) => listeners.push(l),
  };
}
