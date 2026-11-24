// 卓の絵（ドット絵）とサイコロの動き。小さな canvas に描いて、CSS で拡大する（image-rendering: pixelated）。
// 絵はすべてコードで描く。色は app.css の --scene-* から読む（ライト・ダークで変わる）。
// 出目はサーバーが決めたもので、ここでは、その目に向かって転がる様子を見せるだけ。

import { clack } from "./sfx";

export const W = 200;
export const H = 112;
const CX = 100; // 丼の中心
const CY = 62;
const IN_RX = 54; // 丼の内側（サイコロが動ける範囲）
const IN_RY = 22;
const DIE = 12;
const TUMBLE_MS = 1300;
const PAUSE_MS = 650;

type Palette = Record<
  "wood" | "woodDark" | "grain" | "shadow" | "rim" | "rimLight" | "rimDark" | "bowl" | "bowlDeep" | "dieFace" | "dieSide" | "dieEdge" | "pip" | "pipOne" | "spark",
  string
>;

function readPalette(): Palette {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(`--scene-${name}`).trim();
  return {
    wood: v("wood"),
    woodDark: v("wood-dark"),
    grain: v("grain"),
    shadow: v("shadow"),
    rim: v("rim"),
    rimLight: v("rim-light"),
    rimDark: v("rim-dark"),
    bowl: v("bowl"),
    bowlDeep: v("bowl-deep"),
    dieFace: v("die-face"),
    dieSide: v("die-side"),
    dieEdge: v("die-edge"),
    pip: v("pip"),
    pipOne: v("pip-one"),
    spark: v("spark"),
  };
}

// 決まった並びの乱数（木目を毎回同じ形にする）
function seeded(seed: number) {
  return () => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed / 0x7fffffff;
  };
}

interface Die {
  x: number;
  y: number;
  z: number; // 高さ（0 で丼の底）
  vx: number;
  vy: number;
  vz: number;
  face: number;
  final: number;
  edge: boolean; // 転がっている途中の、傾いた見た目
}

export interface Throw {
  dice: number[];
  label: string; // 役の名前（例: 4の目・目なし）
  who: string;
  seat: string; // 振った人の ID
}

const PIPS: Record<number, [number, number][]> = {
  1: [],
  2: [[2, 2], [8, 8]],
  3: [[2, 2], [5, 5], [8, 8]],
  4: [[2, 2], [8, 2], [2, 8], [8, 8]],
  5: [[2, 2], [8, 2], [5, 5], [2, 8], [8, 8]],
  6: [[2, 2], [8, 2], [2, 5], [8, 5], [2, 8], [8, 8]],
};

export class Scene {
  private ctx: CanvasRenderingContext2D;
  private pal: Palette;
  private table: ImageData | null = null;
  private dice: Die[];
  private queue: Throw[] = [];
  private current: Throw | null = null;
  private startedAt = 0;
  private pauseUntil = 0;
  private sparks: { x: number; y: number; vx: number; vy: number; life: number }[] = [];
  private frame = 0;
  private reduced = matchMedia("(prefers-reduced-motion: reduce)");

  /** 役が決まったとき（ラベルの表示）と、転がるものがなくなったとき（結果の表示）に呼ぶ。 */
  onLabel: (t: Throw | null) => void = () => {};
  onIdle: () => void = () => {};

  constructor(canvas: HTMLCanvasElement) {
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas が使えません");
    this.ctx = ctx;
    this.pal = readPalette();
    this.dice = [4, 5, 6].map((f, i) => this.restingDie(f, i));
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this.repaint());
    this.draw();
  }

  /** 色が変わったとき（テーマの切り替え）に描き直す。 */
  repaint() {
    this.pal = readPalette();
    this.table = null;
    this.draw();
  }

  get idle(): boolean {
    return !this.current && this.queue.length === 0;
  }

  private restingDie(face: number, i: number): Die {
    return { x: (i - 1) * 18, y: (i % 2) * 5 - 2, z: 0, vx: 0, vy: 0, vz: 0, face, final: face, edge: false };
  }

  /** 振った目を、順に転がして見せる（目なしの振り直しも1回ずつ）。 */
  play(throws: Throw[]) {
    this.queue.push(...throws);
    if (!this.current) this.next(performance.now());
  }

  /** 動きを見せずに、いまの目を置く（途中から開いた人・視差効果を減らす設定）。 */
  show(dice: number[] | null) {
    this.queue = [];
    this.current = null;
    this.dice = (dice ?? [4, 5, 6]).map((f, i) => this.restingDie(f, i));
    this.onLabel(null); // 前の卓の役の名前を残さない
    this.draw();
  }

  /** 総取りのとき、丼から光の粒を少しだけ出す。 */
  celebrate() {
    if (this.reduced.matches) return;
    const r = seeded(Date.now() & 0xffff);
    for (let i = 0; i < 40; i++) {
      const a = r() * Math.PI * 2;
      const s = 0.6 + r() * 1.6;
      this.sparks.push({ x: CX, y: CY - 6, vx: Math.cos(a) * s, vy: Math.sin(a) * s - 1.4, life: 50 + r() * 30 });
    }
    this.loop();
  }

  private next(now: number) {
    const t = this.queue.shift() ?? null;
    this.current = t;
    if (!t) {
      this.onIdle();
      return;
    }
    this.onLabel(null);
    if (this.reduced.matches) {
      this.dice = t.dice.map((f, i) => this.restingDie(f, i));
      this.finishThrow(now);
      return;
    }
    const r = Math.random;
    this.dice = t.dice.map((final, i) => ({
      x: -40 + i * 6 + r() * 8,
      y: -14 + r() * 6,
      z: 34 + i * 6 + r() * 6,
      vx: 1.2 + r() * 1.4,
      vy: 0.4 + r() * 0.8,
      vz: 0,
      face: 1 + Math.floor(r() * 6),
      final,
      edge: true,
    }));
    this.startedAt = now;
    this.loop();
  }

  private finishThrow(now: number) {
    for (const d of this.dice) {
      d.face = d.final;
      d.edge = false;
      d.z = 0;
    }
    if (this.current) this.onLabel(this.current);
    this.pauseUntil = now + (this.reduced.matches ? 0 : PAUSE_MS);
    this.draw();
    this.loop();
  }

  private running = false;

  private loop() {
    if (this.running) return;
    this.running = true;
    const step = (now: number) => {
      const busy = this.tick(now);
      this.draw();
      if (busy) requestAnimationFrame(step);
      else this.running = false;
    };
    requestAnimationFrame(step);
  }

  /** 1コマ進める。まだ動くものがあれば true。 */
  private tick(now: number): boolean {
    this.frame++;
    for (const s of this.sparks) {
      s.x += s.vx;
      s.y += s.vy;
      s.vy += 0.06;
      s.life--;
    }
    this.sparks = this.sparks.filter((s) => s.life > 0);
    if (!this.current) return this.sparks.length > 0;
    const t = now - this.startedAt;
    if (t > TUMBLE_MS) {
      if (this.dice.some((d) => d.edge)) {
        this.finishThrow(now);
        return true;
      }
      if (now >= this.pauseUntil) this.next(now);
      return Boolean(this.current) || this.sparks.length > 0;
    }
    const settling = t > TUMBLE_MS - 320;
    for (const d of this.dice) {
      d.vz -= 0.55;
      d.z += d.vz;
      if (d.z <= 0) {
        d.z = 0;
        if (d.vz < -1.2) clack(Math.min(1, -d.vz / 6));
        d.vz = -d.vz * 0.45;
        d.vx *= 0.82;
        d.vy *= 0.82;
      }
      d.x += d.vx;
      d.y += d.vy;
      // 丼の内側の縁ではね返る
      const k = (d.x / (IN_RX - DIE / 2)) ** 2 + (d.y / (IN_RY - DIE / 3)) ** 2;
      if (k > 1) {
        const s = 1 / Math.sqrt(k);
        d.x *= s;
        d.y *= s;
        const nx = d.x / (IN_RX * IN_RX);
        const ny = d.y / (IN_RY * IN_RY);
        const nl = Math.hypot(nx, ny) || 1;
        const dot = (d.vx * nx + d.vy * ny) / nl;
        d.vx -= 1.7 * dot * (nx / nl);
        d.vy -= 1.7 * dot * (ny / nl);
        if (Math.abs(dot) > 0.8 && d.z < 4) clack(Math.abs(dot) / 4);
      }
      if (settling) {
        d.vx *= 0.8;
        d.vy *= 0.8;
        d.face = d.final;
        d.edge = d.z > 1;
      } else if (this.frame % 4 === 0) {
        d.face = 1 + Math.floor(Math.random() * 6);
        d.edge = !d.edge || d.z > 2;
      }
    }
    this.separate();
    return true;
  }

  /** サイコロどうしが重ならないように、少し押し離す。 */
  private separate() {
    for (let i = 0; i < this.dice.length; i++)
      for (let j = i + 1; j < this.dice.length; j++) {
        const a = this.dice[i];
        const b = this.dice[j];
        const dx = b.x - a.x;
        const dy = (b.y - a.y) * 1.6;
        const dist = Math.hypot(dx, dy);
        if (dist > 0 && dist < DIE + 1) {
          const push = (DIE + 1 - dist) / 2;
          a.x -= (dx / dist) * push;
          b.x += (dx / dist) * push;
          a.y -= (dy / dist) * push * 0.6;
          b.y += (dy / dist) * push * 0.6;
        }
      }
  }

  // ------------------------------ 描く ------------------------------ //
  private ellipse(cx: number, cy: number, rx: number, ry: number, color: string, from = -Infinity, to = Infinity) {
    const c = this.ctx;
    c.fillStyle = color;
    for (let y = Math.ceil(-ry); y <= Math.floor(ry); y++) {
      if (cy + y < from || cy + y > to) continue;
      const half = Math.round(rx * Math.sqrt(Math.max(0, 1 - (y / ry) ** 2)));
      c.fillRect(Math.round(cx - half), Math.round(cy + y), half * 2, 1);
    }
  }

  private drawTable() {
    // 木の卓と丼は動かないので、一度描いて取っておく
    if (this.table) {
      this.ctx.putImageData(this.table, 0, 0);
      return;
    }
    const c = this.ctx;
    const p = this.pal;
    const r = seeded(7);
    for (let y = 0; y < H; y += 8) {
      c.fillStyle = (y / 8) % 2 ? p.wood : p.woodDark;
      c.fillRect(0, y, W, 8);
      c.fillStyle = p.grain;
      c.fillRect(0, y + 7, W, 1);
      for (let i = 0; i < 9; i++) c.fillRect(Math.floor(r() * W), y + 1 + Math.floor(r() * 5), 4 + Math.floor(r() * 14), 1);
    }
    // 丼の影・外側・縁・内側
    this.ellipse(CX + 3, CY + 9, 64, 26, p.shadow);
    this.ellipse(CX, CY + 4, 62, 28, p.rimDark);
    this.ellipse(CX, CY, 62, 28, p.rim);
    this.ellipse(CX, CY, 62, 28, p.rimLight, -Infinity, CY - 22);
    this.ellipse(CX, CY, IN_RX + 2, IN_RY + 3, p.rimDark);
    this.ellipse(CX, CY + 1, IN_RX, IN_RY + 1, p.bowl);
    this.ellipse(CX, CY + 1, IN_RX, IN_RY + 1, p.bowlDeep, -Infinity, CY - 12);
    this.table = c.getImageData(0, 0, W, H);
  }

  private drawDie(d: Die) {
    const c = this.ctx;
    const p = this.pal;
    const x = Math.round(CX + d.x - DIE / 2);
    const y = Math.round(CY + d.y - DIE / 2 - d.z);
    // 影（高く上がるほど小さく薄く）
    const shrink = Math.min(4, Math.floor(d.z / 8));
    c.fillStyle = p.shadow;
    c.fillRect(Math.round(CX + d.x - DIE / 2) + 1 + shrink, Math.round(CY + d.y + DIE / 2) - 1, DIE - 2 - shrink * 2, 2);
    if (d.edge) {
      // 傾いた見た目: 上の面を縮め、側面を見せる
      c.fillStyle = p.dieEdge;
      c.fillRect(x + 1, y + 2, DIE - 2, DIE - 2);
      c.fillStyle = p.dieFace;
      c.fillRect(x + 1, y + 2, DIE - 2, 6);
      c.fillStyle = p.dieSide;
      c.fillRect(x + 1, y + 8, DIE - 2, 3);
      c.fillStyle = d.face === 1 ? p.pipOne : p.pip;
      c.fillRect(x + 5, y + 4, 2, 2);
      return;
    }
    c.fillStyle = p.dieEdge;
    c.fillRect(x + 1, y, DIE - 2, DIE);
    c.fillRect(x, y + 1, DIE, DIE - 2);
    c.fillStyle = p.dieFace;
    c.fillRect(x + 1, y + 1, DIE - 2, DIE - 3);
    c.fillStyle = p.dieSide;
    c.fillRect(x + 1, y + DIE - 2, DIE - 2, 1);
    if (d.face === 1) {
      c.fillStyle = p.pipOne;
      c.fillRect(x + 4, y + 4, 4, 3);
      c.fillRect(x + 5, y + 3, 2, 5);
      return;
    }
    c.fillStyle = p.pip;
    for (const [px, py] of PIPS[d.face]) c.fillRect(x + px, y + py - (py > 5 ? 1 : 0), 2, 2);
  }

  draw() {
    this.drawTable();
    // 奥のサイコロから描く
    for (const d of [...this.dice].sort((a, b) => a.y - b.y)) this.drawDie(d);
    this.ctx.fillStyle = this.pal.spark;
    for (const s of this.sparks) if (s.life % 6 < 4) this.ctx.fillRect(Math.round(s.x), Math.round(s.y), 1, 1);
  }
}
