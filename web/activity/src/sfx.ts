// 効果音。録音した音は使わず、Web Audio でその場で合成する（サイコロが丼に当たる音・勝ち・負け）。
// 音は、最初に画面を押したあとから鳴る（ブラウザの決まり）。切り替えは端末に覚える。

const KEY = "casino-activity-sound";

let ctx: AudioContext | null = null;
let enabled = readEnabled();

function readEnabled(): boolean {
  try {
    return localStorage.getItem(KEY) !== "off";
  } catch {
    return true;
  }
}

export function soundOn(): boolean {
  return enabled;
}

export function setSound(on: boolean) {
  enabled = on;
  try {
    localStorage.setItem(KEY, on ? "on" : "off");
  } catch {
    // 保存できない環境（プライベートな窓など）では、この回だけ覚える
  }
}

/** 画面を押したときに呼ぶ。音の準備をする。 */
export function unlock() {
  if (!ctx) {
    try {
      ctx = new AudioContext();
    } catch {
      return;
    }
  }
  if (ctx.state === "suspended") void ctx.resume();
}

function ready(): AudioContext | null {
  return enabled && ctx && ctx.state === "running" ? ctx : null;
}

/** サイコロが丼に当たる音。strength は 0〜1。 */
export function clack(strength: number) {
  const a = ready();
  if (!a) return;
  const len = Math.floor(a.sampleRate * 0.05);
  const buf = a.createBuffer(1, len, a.sampleRate);
  const data = buf.getChannelData(0);
  for (let i = 0; i < len; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / len) ** 3;
  const src = a.createBufferSource();
  src.buffer = buf;
  const band = a.createBiquadFilter();
  band.type = "bandpass";
  band.frequency.value = 1800 + Math.random() * 1600;
  band.Q.value = 6;
  const gain = a.createGain();
  gain.gain.value = 0.15 + 0.5 * Math.min(1, strength);
  src.connect(band).connect(gain).connect(a.destination);
  src.start();
}

function notes(freqs: number[], step: number, type: OscillatorType, volume: number) {
  const a = ready();
  if (!a) return;
  freqs.forEach((f, i) => {
    const t = a.currentTime + i * step;
    const osc = a.createOscillator();
    osc.type = type;
    osc.frequency.value = f;
    const g = a.createGain();
    g.gain.setValueAtTime(volume, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + step * 1.8);
    osc.connect(g).connect(a.destination);
    osc.start(t);
    osc.stop(t + step * 2);
  });
}

export function win() {
  notes([523, 659, 784, 1047], 0.09, "square", 0.06);
}

export function lose() {
  notes([294, 220], 0.16, "triangle", 0.12);
}
