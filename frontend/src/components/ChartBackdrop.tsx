import { useEffect, useRef } from "react";
import type { Theme } from "../storage";

/**
 * Chart ambience for the bridge: abstract isobath contours on canvas plus
 * graticule rails labelled with the REAL box coordinates (78.5-82.0 E,
 * 8.0-12.0 N). Decorative, and deliberately number-free beyond the frame
 * labels -- the reference mock drew random sounding depths here, which on
 * our water would read as data. Real numbers live in answers, never in
 * wallpaper.
 */

function seeded(a: number): () => number {
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Shoal {
  x: number;
  y: number;
  r0: number;
  rings: number;
  step: number;
  amp: number[];
  ph: number[];
}

function makeShoals(): Shoal[] {
  const rnd = seeded(20260907);
  const spec = [
    { x: 0.16, y: 0.24, r0: 0.05, rings: 11, step: 0.036 },
    { x: 0.83, y: 0.68, r0: 0.045, rings: 12, step: 0.033 },
    { x: 0.52, y: 1.02, r0: 0.06, rings: 8, step: 0.04 },
    { x: 0.05, y: 0.92, r0: 0.03, rings: 7, step: 0.03 },
  ];
  return spec.map((c) => ({
    ...c,
    amp: [0.11, 0.07, 0.045, 0.028].map((a) => a * (0.6 + rnd() * 0.9)),
    ph: [0, 1, 2, 3].map(() => rnd() * Math.PI * 2),
  }));
}

const SHOALS = makeShoals();

const dm = (minutes: number, hemi: string) =>
  `${Math.floor(minutes / 60)}°${String(Math.round(minutes % 60)).padStart(2, "0")}′${hemi}`;

export default function ChartBackdrop({ theme }: { theme: Theme }) {
  const canvas = useRef<HTMLCanvasElement>(null);

  // Slow current drift: the phase of every contour advances a fraction
  // each tick, so the whole field breathes like water. Throttled to ~8
  // redraws a second (plenty for a background), paused automatically when
  // the tab hides (rAF stops) and off entirely under reduced-motion.
  // Redraws on mount, on resize, and on theme switch (tokens live in CSS,
  // so the stroke colour is re-read after the attribute lands).
  const reduceMotion =
    typeof matchMedia !== "undefined" &&
    matchMedia("(prefers-reduced-motion: reduce)").matches;
  useEffect(() => {
    let phase = 0;
    const draw = (ph = 0) => {
      const cv = canvas.current;
      if (!cv) return;
      const parent = cv.parentElement;
      if (!parent) return;
      const w = parent.clientWidth;
      const h = parent.clientHeight;
      if (!w || !h) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      const g = cv.getContext("2d");
      if (!g) return;
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, w, h);

      const css = (n: string) =>
        getComputedStyle(document.documentElement).getPropertyValue(n).trim();
      const colour = css("--isobath") || "#c1d3d5";
      const s = Math.max(w, h);
      const N = 150;
      g.lineWidth = 1;
      for (const c of SHOALS) {
        const cx = c.x * w;
        const cy = c.y * h;
        for (let k = 0; k < c.rings; k++) {
          const rr = (c.r0 + k * c.step) * s;
          g.beginPath();
          for (let i = 0; i <= N; i++) {
            const t = (i / N) * Math.PI * 2;
            let m = 1;
            for (let q = 0; q < 4; q++) m += c.amp[q] * Math.sin((q + 2) * t + c.ph[q] + k * 0.2 + ph);
            const x = cx + Math.cos(t) * rr * m;
            const y = cy + Math.sin(t) * rr * m * 0.8;
            if (i) g.lineTo(x, y);
            else g.moveTo(x, y);
          }
          g.closePath();
          g.globalAlpha = 0.85 * (k % 5 === 0 ? 1 : 0.62);
          g.strokeStyle = colour;
          g.stroke();
        }
      }
      g.globalAlpha = 1;
    };
    // Let the theme attribute commit before reading its tokens.
    let raf = 0;
    let lastFrame = 0;
    if (reduceMotion) {
      raf = requestAnimationFrame(() => draw(0));
    } else {
      // One rAF loop, one redraw per ~120 ms: a full 60 fps redraw of
      // ~6k segments would be pure battery burn for wallpaper.
      const loop = (now: number) => {
        if (now - lastFrame > 120) {
          lastFrame = now;
          phase += 0.06;
          draw(phase);
        }
        raf = requestAnimationFrame(loop);
      };
      raf = requestAnimationFrame(loop);
    }
    let t = 0;
    const debounced = () => {
      window.clearTimeout(t);
      t = window.setTimeout(() => draw(phase), 150);
    };
    window.addEventListener("resize", debounced);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", debounced);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme]);

  // 9 longitude ticks across 78.5-82.0 E, 6 latitude ticks across 12.0-8.0 N.
  const lons = Array.from({ length: 9 }, (_, i) => dm(78.5 * 60 + i * 26.25, "E"));
  const lats = Array.from({ length: 6 }, (_, i) => dm(12 * 60 - i * 48, "N"));

  return (
    <>
      <canvas id="chart" ref={canvas} aria-hidden="true" />
      <div className="grat grat-top" aria-hidden="true">
        {lons.map((l) => (
          <span key={l}>
            <i>{l}</i>
          </span>
        ))}
      </div>
      <div className="grat grat-left" aria-hidden="true">
        {lats.map((l) => (
          <span key={l}>
            <i>{l}</i>
          </span>
        ))}
      </div>
    </>
  );
}
