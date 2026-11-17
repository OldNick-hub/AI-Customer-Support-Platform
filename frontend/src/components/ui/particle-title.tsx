import React, { useEffect, useRef, useState, useCallback } from 'react';

/* ------------------------------------------------------------------ */
/* Maths & Types                                                      */
/* ------------------------------------------------------------------ */

type RGB = [number, number, number];
type Box = { x: number; y: number; w: number; h: number };
type Field = { cx: number; cy: number; rx: number; ry: number };
type Mode = 'smoke' | 'vortex' | 'sand' | 'burst' | 'sweep' | 'absorb';
type Blob = { rgb: RGB; a: number; fx: number; fy: number; fw: number; fh: number };
type Palette = { ink: RGB; page: RGB; blobs: Blob[] };

const MODES: Mode[] = ['smoke', 'vortex', 'sand', 'burst', 'sweep', 'absorb'];
const TIMING = { hold: 2400, field: 450, travel: 1800 };

const LIGHT: Palette = {
  ink: [15, 23, 42],
  page: [248, 250, 252],
  blobs: [
    { rgb: [37, 99, 235], a: 0.65, fx: 0.04, fy: 0.08, fw: 0.65, fh: 0.8 },
    { rgb: [124, 58, 237], a: 0.55, fx: 0.44, fy: 0.22, fw: 0.55, fh: 0.72 },
    { rgb: [2, 132, 199], a: 0.5, fx: 0.3, fy: 0.44, fw: 0.45, fh: 0.58 },
  ],
};

const DARK: Palette = {
  ink: [255, 255, 255],
  page: [7, 10, 18],
  blobs: [
    { rgb: [56, 189, 248], a: 0.85, fx: 0.04, fy: 0.08, fw: 0.65, fh: 0.8 },
    { rgb: [168, 85, 247], a: 0.75, fx: 0.44, fy: 0.22, fw: 0.55, fh: 0.72 },
    { rgb: [52, 211, 153], a: 0.7, fx: 0.3, fy: 0.44, fw: 0.45, fh: 0.58 },
  ],
};

function createRng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const clamp = (t: number) => (t < 0 ? 0 : t > 1 ? 1 : t);
const mix = (a: number, b: number, t: number) => a + (b - a) * t;
const smooth = (t: number) => {
  const c = clamp(t);
  return c * c * (3 - 2 * c);
};
const ease = (t: number) => {
  const c = clamp(t);
  return c < 0.5 ? 4 * c * c * c : 1 - Math.pow(-2 * c + 2, 3) / 2;
};

function createModeBag(seed: number): () => Mode {
  const r = createRng(seed);
  let bag: Mode[] = [];
  let last: Mode | null = null;
  return () => {
    if (!bag.length) {
      bag = MODES.slice();
      for (let i = bag.length - 1; i > 0; i--) {
        const j = Math.floor(r() * (i + 1));
        [bag[i], bag[j]] = [bag[j], bag[i]];
      }
      if (bag[bag.length - 1] === last) [bag[0], bag[bag.length - 1]] = [bag[bag.length - 1], bag[0]];
    }
    last = bag.pop() as Mode;
    return last;
  };
}

function blobStrength(b: Blob, A: Box, x: number, y: number) {
  const bw = b.fw * A.w;
  const bh = b.fh * A.h;
  const cx = A.x + b.fx * A.w + bw / 2;
  const cy = A.y + b.fy * A.h + bh / 2;
  return Math.max(0, 1 - Math.hypot((x - cx) / (bw / 2), (y - cy) / (bh / 2))) * b.a;
}

function rimMask(A: Box, x: number, y: number) {
  const r = Math.hypot((x - (A.x + A.w / 2)) / (A.w / 2), (y - (A.y + A.h / 2)) / (A.h / 2));
  return r < 0.55 ? 1 : r >= 1 ? 0 : 1 - (r - 0.55) / 0.45;
}

function glowAt(pal: Palette, A: Box, x: number, y: number): { c: RGB; s: number } {
  let [r, g, b] = pal.blobs[0]?.rgb || [56, 189, 248];
  let vis = 0.4;
  const m = rimMask(A, x, y);
  for (const bl of pal.blobs) {
    const s = blobStrength(bl, A, x, y) * m;
    r = mix(r, bl.rgb[0], s);
    g = mix(g, bl.rgb[1], s);
    b = mix(b, bl.rgb[2], s);
    vis = Math.max(vis, s);
  }
  return { c: [r, g, b], s: Math.max(0.35, vis) };
}

function sampleFieldPoint(pal: Palette, rand: () => number, f: Field, A: Box) {
  let best: { x: number; y: number; c: RGB; s: number } | null = null;
  for (let k = 0; k < 4; k++) {
    const a = rand() * 6.2832;
    const q = Math.pow(rand(), 0.6);
    const x = f.cx + Math.cos(a) * f.rx * q;
    const y = f.cy + Math.sin(a) * f.ry * q;
    const g = glowAt(pal, A, x, y);
    if (!best || g.s > best.s) best = { x, y, c: g.c, s: g.s };
  }
  return best as { x: number; y: number; c: RGB; s: number };
}

/* ------------------------------------------------------------------ */
/* Canvas Engine                                                      */
/* ------------------------------------------------------------------ */

type Cell = {
  hx: number;
  hy: number;
  nx: number;
  ny: number;
  r1: number;
  r2: number;
  r3: number;
  size: number;
  fx: number;
  fy: number;
  fc: RGB;
  lt: number;
  bend: number;
  pa: number;
};
type Phase = 'text' | 'dissolving' | 'field' | 'gathering';

function plan(mode: Mode, p: Cell, W: number, H: number) {
  switch (mode) {
    case 'smoke':
      return { delay: (1 - clamp(p.ny)) * 0.35 + p.r3 * 0.25, bend: (p.r1 - 0.5) * 160 };
    case 'vortex':
      return {
        delay: (Math.hypot(p.hx - W / 2, p.hy - H / 2) / Math.max(1, W)) * 0.85 + p.r3 * 0.15,
        bend: (p.r2 > 0.5 ? 1 : -1) * (110 + p.r1 * 150),
      };
    case 'sand':
      return { delay: p.nx * 0.55 + p.r3 * 0.18, bend: (p.r1 - 0.5) * 70 };
    case 'burst':
      return { delay: p.r3 * 0.15, bend: (p.r2 - 0.5) * 35 };
    case 'sweep':
      return { delay: (1 - p.nx) * 0.55 + p.r3 * 0.18, bend: -(70 + p.r1 * 100) };
    default:
      return { delay: p.r1 * 0.5, bend: (p.r2 - 0.5) * 110 };
  }
}

function bowAt(a: [number, number], b: [number, number], bend: number, t: number) {
  const e = ease(t);
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const L = Math.hypot(dx, dy) || 1;
  const bow = Math.sin(e * Math.PI) * bend;
  return [mix(a[0], b[0], e) + (-dy / L) * bow, mix(a[1], b[1], e) + (dx / L) * bow, e] as const;
}

const rgbStr = (a: RGB, b: RGB, t: number) => {
  const c = (i: number) => Math.max(0, Math.min(255, Math.round(mix(a[i], b[i], t))));
  return `rgb(${c(0)},${c(1)},${c(2)})`;
};

export type Engine = {
  setActive: (on: boolean) => void;
  resize: () => void;
  triggerSwirl: () => void;
  destroy: () => void;
};

const INTRO_LEAD = 0.12;
const FONT_STACK = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';

function createEngine(cv: HTMLCanvasElement, text: string, pal: Palette): Engine | null {
  const ctx = cv.getContext('2d');
  if (!ctx) return null;
  const textCv = document.createElement('canvas');
  const nextMode = createModeBag((Date.now() & 0xffff) ^ 0x5a5a);
  let W = 0;
  let H = 0;
  let dpr = 1;
  let field: Field = { cx: 0, cy: 0, rx: 0, ry: 0 };
  let box: Box = { x: 0, y: 0, w: 0, h: 0 };
  let parts: Cell[] = [];
  let phase: Phase = 'gathering';
  let mode: Mode = nextMode();
  let phaseStart = 0;
  let raf = 0;
  let timer = 0;
  let intro = true;
  let active = true;
  let destroyed = false;

  function reroll() {
    const r = createRng((Math.random() * 1e9) | 0);
    for (const p of parts) {
      const q = sampleFieldPoint(pal, r, field, box);
      p.fx = q.x;
      p.fy = q.y;
      p.fc = q.c;
    }
  }

  function build() {
    W = cv.clientWidth || cv.parentElement?.clientWidth || window.innerWidth || 800;
    H = cv.clientHeight || cv.parentElement?.clientHeight || 340;
    dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.max(1, Math.round(W * dpr));
    cv.height = Math.max(1, Math.round(H * dpr));
    textCv.width = cv.width;
    textCv.height = cv.height;
    ctx!.setTransform(1, 0, 0, 1, 0, 0);

    const maxFsByH = Math.max(16, Math.floor(H * 0.4));
    let fs = Math.min(maxFsByH, W >= 1100 ? 38 : Math.max(18, Math.min(34, Math.floor(W * 0.042))));
    const ty = H / 2;
    box = { x: 0, y: 0, w: W, h: H };
    field = { cx: W / 2, cy: H / 2, rx: Math.min(580, W * 0.45), ry: Math.min(280, H * 0.38) };

    const o = textCv.getContext('2d', { willReadFrequently: true }) as CanvasRenderingContext2D;
    o.setTransform(dpr, 0, 0, dpr, 0, 0);
    o.clearRect(0, 0, W, H);
    o.font = `700 ${fs}px ${FONT_STACK}`;

    // Auto-fit check
    let m = o.measureText(text);
    if (m.width > W * 0.92 && W > 120) {
      fs = Math.max(16, Math.floor(fs * ((W * 0.92) / m.width)));
      o.font = `700 ${fs}px ${FONT_STACK}`;
      m = o.measureText(text);
    }

    o.textAlign = 'center';
    o.textBaseline = 'middle';
    o.fillStyle = `rgba(${pal.ink.join(',')},.98)`;
    o.fillText(text, W / 2, ty);

    const tw = m.width;
    const X0 = Math.max(0, Math.floor((W / 2 - tw / 2 - 16) * dpr));
    const X1 = Math.min(cv.width, Math.ceil((W / 2 + tw / 2 + 16) * dpr));
    const Y0 = Math.max(0, Math.floor((ty - fs - 12) * dpr));
    const Y1 = Math.min(cv.height, Math.ceil((ty + fs + 12) * dpr));

    const d = o.getImageData(0, 0, cv.width, cv.height).data;
    let cs = Math.max(2, Math.round(2.5 * dpr));
    let pts: number[][] = [];

    for (;;) {
      pts = [];
      for (let y = Y0; y < Y1; y += cs) {
        for (let x = X0; x < X1; x += cs) {
          const w = Math.min(cs, cv.width - x);
          const h = Math.min(cs, cv.height - y);
          let hit = false;
          for (let yy = y; yy < y + h && !hit; yy++) {
            for (let xx = x; xx < x + w; xx++) {
              if (d[(yy * cv.width + xx) * 4 + 3] > 15) {
                hit = true;
                break;
              }
            }
          }
          if (hit) pts.push([(x + w / 2) / dpr, (y + h / 2) / dpr]);
        }
      }
      if (pts.length <= 2500) break;
      cs++;
    }

    // High-reliability fallback if pixel sampling returns too few points
    if (pts.length < 150) {
      const stepX = Math.max(3, Math.floor((tw * dpr) / 80));
      const stepY = Math.max(3, Math.floor((fs * dpr) / 8));
      for (let y = Y0; y < Y1; y += stepY) {
        for (let x = X0; x < X1; x += stepX) {
          if (Math.random() > 0.35) {
            pts.push([x / dpr, y / dpr]);
          }
        }
      }
    }

    let minX = 1e9;
    let maxX = -1e9;
    for (const p of pts) {
      if (p[0] < minX) minX = p[0];
      if (p[0] > maxX) maxX = p[0];
    }

    const r = createRng(7);
    parts = pts.map((p) => {
      const u = r();
      const size = u < 0.4 ? 2.4 + r() * 1.2 : u < 0.85 ? 3.2 + r() * 1.6 : 4.8 + r() * 2.8;
      return {
        hx: p[0],
        hy: p[1],
        nx: (p[0] - minX) / Math.max(1, maxX - minX),
        ny: (p[1] - (ty - fs / 2)) / fs,
        r1: r(),
        r2: r(),
        r3: r(),
        size,
        fx: 0,
        fy: 0,
        fc: [0, 0, 0] as RGB,
        lt: 0,
        bend: 0,
        pa: 0,
      };
    });

    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    reroll();
  }

  function drawRealText() {
    const c = ctx as CanvasRenderingContext2D;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, cv.width, cv.height);
    c.globalAlpha = 1;
    c.drawImage(textCv, 0, 0);
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function frame(now: number) {
    raf = 0;
    const c = ctx as CanvasRenderingContext2D;
    const lead = intro && phase === 'gathering' ? INTRO_LEAD : 0;
    const prog = clamp((now - phaseStart) / TIMING.travel + lead);
    const out = phase === 'dissolving';

    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, cv.width, cv.height);

    // Smoothly blend the crisp text as particles arrive or depart
    const textAlpha = out ? 1 - smooth(prog / 0.35) : smooth((prog - 0.25) / 0.75);
    if (textAlpha > 0.01) {
      c.globalAlpha = textAlpha;
      c.drawImage(textCv, 0, 0);
    }

    // Update particle trajectory calculations
    for (const p of parts) {
      const pl = plan(mode, p, W, H);
      const lt = clamp((prog - pl.delay * 0.5) / 0.5);
      p.lt = lt;
      p.bend = pl.bend;
      p.pa = textAlpha;
    }

    // Draw the glowing dust particles in motion
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.shadowBlur = 8;

    for (const p of parts) {
      const lt = p.lt;
      let qx = 0;
      let qy = 0;
      let alpha = 0;
      let e = 0;

      if (out) {
        // Dissolving outwards
        if (lt <= 0) continue;
        if (lt >= 1) {
          qx = p.fx;
          qy = p.fy;
          e = 0;
          alpha = Math.max(0.12, 0.45 * (1 - p.r1 * 0.3));
        } else {
          const q = bowAt([p.hx, p.hy], [p.fx, p.fy], p.bend, lt);
          qx = q[0];
          qy = q[1];
          e = q[2];
          alpha = (0.5 + 0.5 * (1 - smooth((e - 0.4) / 0.6))) * (1 - smooth((lt - 0.88) / 0.12));
        }
      } else {
        // Gathering inwards
        if (lt <= 0) {
          // Frame 0: clearly visible cosmic dust floating in the radial field
          qx = p.fx + Math.sin(now * 0.0014 + p.r1 * 6.28) * 4;
          qy = p.fy + Math.cos(now * 0.0014 + p.r2 * 6.28) * 4;
          e = 0;
          alpha = (0.45 + 0.55 * p.r3) * (1 - p.pa);
        } else if (lt < 1) {
          const q = bowAt([p.fx, p.fy], [p.hx, p.hy], -p.bend, lt);
          qx = q[0];
          qy = q[1];
          e = 1 - q[2];
          alpha = (0.6 + 0.4 * smooth(lt / 0.25)) * (1 - p.pa * 0.85);
        } else {
          // Docked at final text position
          qx = p.hx;
          qy = p.hy;
          e = 1;
          alpha = Math.max(0, 1 - p.pa);
        }
      }

      if (alpha <= 0.004) continue;
      c.globalAlpha = Math.min(1, alpha);
      const particleColor = rgbStr(p.fc, pal.ink, smooth(e / 0.6));
      c.fillStyle = particleColor;
      c.shadowColor = '#38bdf8';

      c.beginPath();
      c.arc(qx, qy, (p.size * mix(1, 1.5, smooth(e))) / 2, 0, 6.2832);
      c.fill();
    }

    c.shadowBlur = 0;
    c.globalAlpha = 1;

    if (prog < 1) raf = requestAnimationFrame(frame);
    else advance();
  }

  function frameText(now: number) {
    raf = 0;
    const c = ctx as CanvasRenderingContext2D;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, cv.width, cv.height);
    c.globalAlpha = 0.96;
    c.drawImage(textCv, 0, 0);

    // Continuous celestial shimmer particles hovering over the text letters
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.shadowBlur = 8;
    const step = Math.max(1, Math.floor(parts.length / 50));
    for (let i = 0; i < parts.length; i += step) {
      const p = parts[i];
      if (!p) continue;
      const qx = p.hx + Math.sin(now * 0.0022 + p.r1 * 6.28) * 3;
      const qy = p.hy + Math.cos(now * 0.0022 + p.r2 * 6.28) * 2;
      const shimmerAlpha = 0.3 + 0.45 * Math.sin(now * 0.0035 + p.r3 * 6.28);
      c.globalAlpha = Math.max(0.08, shimmerAlpha);
      c.fillStyle = '#38bdf8';
      c.shadowColor = '#38bdf8';
      c.beginPath();
      c.arc(qx, qy, p.size * 0.65, 0, 6.2832);
      c.fill();
    }
    c.shadowBlur = 0;
    c.globalAlpha = 1;
    if (phase === 'text' && active && !destroyed) {
      raf = requestAnimationFrame(frameText);
    }
  }

  function frameField(now: number) {
    raf = 0;
    const c = ctx as CanvasRenderingContext2D;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.clearRect(0, 0, cv.width, cv.height);
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.shadowBlur = 10;

    for (const p of parts) {
      const qx = p.fx + Math.sin(now * 0.0018 + p.r1 * 6.28) * 6;
      const qy = p.fy + Math.cos(now * 0.0018 + p.r2 * 6.28) * 6;
      const alpha = 0.35 + 0.45 * p.r3;
      c.globalAlpha = alpha;
      const particleColor = '#38bdf8';
      c.fillStyle = particleColor;
      c.shadowColor = particleColor;
      c.beginPath();
      c.arc(qx, qy, p.size * 0.85, 0, 6.2832);
      c.fill();
    }
    c.shadowBlur = 0;
    c.globalAlpha = 1;
    if (phase === 'field' && active && !destroyed) {
      raf = requestAnimationFrame(frameField);
    }
  }

  function stopWork() {
    clearTimeout(timer);
    cancelAnimationFrame(raf);
    raf = 0;
  }

  function enter(next: Phase) {
    phase = next;
    phaseStart = performance.now();
    stopWork();
    if (!active || destroyed) return;
    if (next === 'text') {
      intro = false;
      frameText(performance.now());
      timer = window.setTimeout(advance, TIMING.hold);
    } else if (next === 'field') {
      frameField(performance.now());
      timer = window.setTimeout(advance, TIMING.field);
    } else {
      if (next === 'dissolving') {
        mode = nextMode();
        reroll();
      }
      frame(performance.now());
    }
  }

  function advance() {
    enter(phase === 'text' ? 'dissolving' : phase === 'dissolving' ? 'field' : phase === 'field' ? 'gathering' : 'text');
  }

  build();
  enter('gathering');

  return {
    setActive(on) {
      if (on === active || destroyed) return;
      active = on;
      if (on) enter(phase);
      else stopWork();
    },
    resize() {
      if (destroyed) return;
      const elapsed = performance.now() - phaseStart;
      stopWork();
      build();
      phaseStart = performance.now() - elapsed;
      if (active) {
        if (phase === 'text') drawRealText();
        else frame(performance.now());
      }
    },
    triggerSwirl() {
      if (destroyed) return;
      enter('dissolving');
    },
    destroy() {
      destroyed = true;
      active = false;
      stopWork();
    },
  };
}

/* ------------------------------------------------------------------ */
/* React Component                                                    */
/* ------------------------------------------------------------------ */

/** Dark when <html> carries .dark or data-theme="dark"; otherwise follows OS setting */
function useIsDark() {
  const [dark, setDark] = useState(true);
  useEffect(() => {
    const root = document.documentElement;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const read = () => {
      const themeAttr = root.getAttribute('data-theme');
      const explicit =
        root.classList.contains('dark') || themeAttr === 'dark'
          ? true
          : root.classList.contains('light') || themeAttr === 'light'
          ? false
          : null;
      setDark(explicit ?? mq.matches);
    };
    read();
    const mo = new MutationObserver(read);
    mo.observe(root, { attributes: true, attributeFilter: ['class', 'data-theme'] });
    mq.addEventListener('change', read);
    return () => {
      mo.disconnect();
      mq.removeEventListener('change', read);
    };
  }, []);
  return dark;
}

const css = (c: RGB, a: number) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

export interface ParticleTitleProps {
  /** Title text. Accessible heading stays in DOM for screen readers. */
  text?: string;
  as?: 'h1' | 'h2' | 'h3';
  className?: string;
  style?: React.CSSProperties;
  height?: number | string;
  showHint?: boolean;
}

/**
 * A title that dissolves into drifting luminous dust inside a soft glow, then gathers back into crisp text.
 * Six dissolve styles rotate: smoke, vortex, sand, burst, sweep, absorb.
 * Supports click-to-swirl interaction.
 */
export const ParticleTitle: React.FC<ParticleTitleProps> = ({
  text = 'Customer Support. Intelligently Resolved.',
  as = 'h1',
  className = '',
  style,
  height = 340,
  showHint = true,
}) => {
  const HeadingTag = as;
  const dark = useIsDark();
  const pal = dark ? DARK : LIGHT;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<Engine | null>(null);

  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const engine = createEngine(cv, text, dark ? DARK : LIGHT);
    if (!engine) return;
    engineRef.current = engine;

    // Immediately trigger layout measurement
    const rafId = requestAnimationFrame(() => {
      engine.resize();
    });

    let visible = true;
    let size = `${cv.clientWidth}x${cv.clientHeight}`;
    const sync = () => engine.setActive(visible && !document.hidden);
    const io = new IntersectionObserver((entries) => {
      visible = entries[entries.length - 1].isIntersecting;
      sync();
    });
    io.observe(cv);

    let rz = 0;
    const ro = new ResizeObserver(() => {
      const next = `${cv.clientWidth}x${cv.clientHeight}`;
      if (next === size) return;
      size = next;
      clearTimeout(rz);
      rz = window.setTimeout(() => engine.resize(), 100);
    });
    ro.observe(cv);

    // Font readiness check
    if (document.fonts?.ready) {
      document.fonts.ready.then(() => engine.resize());
    }

    document.addEventListener('visibilitychange', sync);
    sync();

    return () => {
      cancelAnimationFrame(rafId);
      document.removeEventListener('visibilitychange', sync);
      clearTimeout(rz);
      io.disconnect();
      ro.disconnect();
      engine.destroy();
      engineRef.current = null;
    };
  }, [dark, text]);

  const handleStageClick = useCallback(() => {
    engineRef.current?.triggerSwirl();
  }, []);

  return (
    <div
      className={`particle-title-stage ${className}`}
      onClick={handleStageClick}
      title="Click to swirl particles"
      style={{
        position: 'relative',
        isolation: 'isolate',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        overflow: 'hidden',
        width: '100%',
        height: typeof height === 'number' ? `${height}px` : height,
        cursor: 'pointer',
        userSelect: 'none',
        ...style,
      }}
    >
      {/* Soft Ambient Radial Background Glow */}
      <div
        aria-hidden="true"
        style={{
          pointerEvents: 'none',
          position: 'absolute',
          inset: 0,
          WebkitMaskImage: 'radial-gradient(closest-side, #000 65%, transparent 100%)',
          maskImage: 'radial-gradient(closest-side, #000 65%, transparent 100%)',
        }}
      >
        {pal.blobs.map((b, i) => {
          const positions = [
            { left: '4%', top: '8%', width: '62%', height: '78%' },
            { left: '44%', top: '22%', width: '54%', height: '70%' },
            { left: '30%', top: '44%', width: '44%', height: '56%' },
          ];
          const pos = positions[i] || positions[0];
          return (
            <div
              key={i}
              style={{
                position: 'absolute',
                borderRadius: '9999px',
                ...pos,
                background: `radial-gradient(closest-side, ${css(b.rgb, b.a)}, ${css(b.rgb, 0)} 100%)`,
                filter: 'blur(32px)',
              }}
            />
          );
        })}
      </div>

      {/* Interactive Physics Canvas - Guaranteed Always Active */}
      <canvas
        ref={canvasRef}
        aria-hidden="true"
        style={{
          pointerEvents: 'none',
          position: 'absolute',
          inset: 0,
          height: '100%',
          width: '100%',
          display: 'block',
        }}
      />

      {/* Screen-reader Semantic Title */}
      <HeadingTag
        style={{
          position: 'absolute',
          margin: -1,
          height: 1,
          width: 1,
          overflow: 'hidden',
          clip: 'rect(0, 0, 0, 0)',
          whiteSpace: 'nowrap',
          border: 0,
          padding: 0,
        }}
      >
        {text}
      </HeadingTag>

      {/* Interactive Swirl Pill Badge */}
      {showHint && (
        <div
          style={{
            position: 'absolute',
            bottom: '12px',
            display: 'inline-flex',
            alignItems: 'center',
            gap: '6px',
            padding: '4px 12px',
            borderRadius: '9999px',
            background: 'rgba(56, 189, 248, 0.1)',
            border: '1px solid rgba(56, 189, 248, 0.25)',
            fontSize: '0.6875rem',
            fontWeight: 600,
            letterSpacing: '0.04em',
            color: '#38bdf8',
            pointerEvents: 'none',
            zIndex: 10,
            boxShadow: '0 2px 10px rgba(56, 189, 248, 0.15)',
          }}
        >
          <span>✨</span>
          <span>Click to trigger particle swirl</span>
        </div>
      )}
    </div>
  );
};

export default ParticleTitle;
