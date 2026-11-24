// サーバー（cogs/activity/server.py）とやり取りする形。ID は桁が落ちないよう文字列で届く。

export type Phase = "lobby" | "rolling" | "folded" | "done" | "failed";

export interface Roll {
  dice: number[];
  hand: string;
}

export interface Seat {
  id: string;
  name: string;
  rolls: Roll[];
}

export interface SeatResult {
  id: string;
  name: string;
  net: number;
  best: boolean;
}

export interface Table {
  phase: Phase;
  bet: number;
  host: string;
  closes_at: number;
  roll_seconds: number;
  max_seats: number;
  min_seats: number;
  rake: number;
  seats: Seat[];
  results: SeatResult[];
}

export type ServerMessage =
  | { type: "hello"; you: string; name: string }
  | { type: "state"; table: Table | null }
  | { type: "error"; text: string; fatal?: boolean };

export type Op = { op: "open"; bet: number } | { op: "sit" | "stand" | "start" | "roll" };

/** 画面とサーバーのつなぎ。本物は WebSocket、開発中は模擬（mock.ts）。 */
export interface Link {
  send(op: Op): void;
  onMessage(listener: (msg: ServerMessage) => void): void;
}
