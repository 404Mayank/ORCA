import { useEffect, useMemo, useRef } from "react";

/**
 * Pointer + typing reactive glow, GPU-cheap by construction:
 * - one window pointermove listener, rAF-throttled, shared per hook user
 * - writes go to CSS custom props (no React state, no re-render)
 * - only opacity and animation playback rate downstream (no layout)
 * - idle cost is zero
 * - no-op under prefers-reduced-motion
 *
 * Usage:
 *   const glow = useGlow<HTMLFormElement>();
 *   <form ref={glow.ref} />           // CSS reads --glow-x/-y/-on/-hot
 *   glow.fill(hasText)                // box has content -> keep it flowing
 *   glow.ping()                       // a keystroke landed -> flow faster
 *
 * Speed, not just brightness, is the reaction. See the "reactive glow"
 * comment in index.css for why the rate is changed through
 * `updatePlaybackRate()` and never through `animation-duration`.
 */

/** Playback rate of the composer's drift in each of the three states. */
const RATE_EMPTY = 0.55;
const RATE_FILLED = 1.1;
const RATE_TYPING = 2.4;

/** How long after the last keystroke the flow stays quickened. */
const HOT_MS = 900;

/** The composer's own animations, by name. Filtering keeps a descendant's
 *  animation -- the send button's breathing, say -- out of the rate change. */
const FLOW = /^g-(drift|swell)/;

export default function useGlow<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const decay = useRef(0);
  const filled = useRef(false);
  const rate = useRef(RATE_EMPTY);

  const glow = useMemo(() => {
    const setRate = (value: number) => {
      rate.current = value;
      const node = ref.current;
      if (!node?.getAnimations) return;
      // subtree: true is what reaches ::after -- the animations live on the
      // pseudo-element, not on the form itself. A browser that does not
      // report pseudo-element animations simply keeps the CSS speed, which
      // is a working composer, just a less reactive one.
      for (const anim of node.getAnimations({ subtree: true })) {
        const name = (anim as unknown as { animationName?: string }).animationName;
        if (!name || !FLOW.test(name)) continue;
        // updatePlaybackRate holds the current position and changes only
        // how fast it advances; assigning playbackRate is the fallback.
        if (typeof anim.updatePlaybackRate === "function") anim.updatePlaybackRate(value);
        else anim.playbackRate = value;
      }
    };

    /** The box has text, or does not. Flowing is the resting state of a
     *  composer with something in it; an empty one barely moves. */
    const fill = (hasText: boolean) => {
      filled.current = hasText;
      ref.current?.style.setProperty("--glow-on", hasText ? "1" : "0");
      // Do not undercut an in-flight keystroke; the decay below restores it.
      if (rate.current !== RATE_TYPING) setRate(hasText ? RATE_FILLED : RATE_EMPTY);
    };

    /** A keystroke landed. Brighten and quicken, then settle back to
     *  whatever the fill state calls for. */
    const ping = () => {
      const node = ref.current;
      if (!node) return;
      node.style.setProperty("--glow-hot", "1");
      setRate(RATE_TYPING);
      window.clearTimeout(decay.current);
      decay.current = window.setTimeout(() => {
        node.style.setProperty("--glow-hot", "0");
        setRate(filled.current ? RATE_FILLED : RATE_EMPTY);
      }, HOT_MS);
    };

    return { ref, ping, fill, setRate };
  }, []);

  useEffect(() => {
    if (typeof matchMedia !== "undefined" && matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return;
    }
    // The animations start at rate 1 the moment the panel paints; an empty
    // composer should be slower than that.
    glow.setRate(RATE_EMPTY);

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
    const timer = decay;
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (raf) cancelAnimationFrame(raf);
      window.clearTimeout(timer.current);
    };
  }, [glow]);

  return glow;
}
