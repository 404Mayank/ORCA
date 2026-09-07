import { useEffect, useRef } from "react";

/**
 * Pointer + typing reactive glow, GPU-cheap by construction:
 * - one window pointermove listener, rAF-throttled, shared per hook user
 * - writes go to CSS custom props (no React state, no re-render)
 * - only opacity/transform downstream (composite-only, no layout)
 * - typing sets --glow-on with a decay timeout; idle cost is zero
 * - no-op under prefers-reduced-motion
 *
 * Usage: const ref = useGlow<HTMLDivElement>(); <div ref={ref} .../>
 * The element's CSS reads var(--glow-x), var(--glow-y), var(--glow-on).
 */
export default function useGlow<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const decay = useRef(0);

  useEffect(() => {
    if (typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return;
    }
    let raf = 0;
    let pending: { x: number; y: number } | null = null;
    const flush = () => {
      raf = 0;
      const el = ref.current;
      if (el && pending) {
        const rect = el.getBoundingClientRect();
        el.style.setProperty("--glow-x", `${((pending.x - rect.left) / Math.max(1, rect.width)).toFixed(3)}`);
        el.style.setProperty("--glow-y", `${((pending.y - rect.top) / Math.max(1, rect.height)).toFixed(3)}`);
      }
      pending = null;
    };
    const onMove = (e: PointerEvent) => {
      pending = { x: e.clientX, y: e.clientY };
      if (!raf) raf = requestAnimationFrame(flush);
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (raf) cancelAnimationFrame(raf);
      window.clearTimeout(decay.current);
    };
  }, []);

  const ping = () => {
    const el = ref.current;
    if (!el) return;
    el.style.setProperty("--glow-on", "1");
    window.clearTimeout(decay.current);
    decay.current = window.setTimeout(() => el.style.setProperty("--glow-on", "0"), 1200);
  };

  return { ref, ping };
}
