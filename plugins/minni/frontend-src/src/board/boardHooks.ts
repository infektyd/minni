// Small hooks shared by the memory board.
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

export interface Size {
  w: number;
  h: number;
}

/**
 * Measured size of a stage element (its content box). The board camera math is
 * expressed relative to the stage rather than the window, so panning/zooming
 * stays correct even though the board lives inside the console shell (offset by
 * the rail + status band).
 */
export function useElementSize(): [React.RefObject<HTMLDivElement | null>, Size] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<Size>({ w: 1, h: 1 });
  // Measure before paint so the first frame uses the real stage size (no
  // zoom-in flash from the 1×1 placeholder).
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setSize((prev) => {
        const w = Math.max(1, r.width);
        const h = Math.max(1, r.height);
        return prev.w === w && prev.h === h ? prev : { w, h };
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size];
}

/** JSON-backed localStorage state with a namespaced key. */
export function usePersistentJSON<T>(
  key: string,
  initial: T,
  validate?: (value: unknown) => T | null,
): [T, (next: T | ((prev: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw == null) return initial;
      const parsed = JSON.parse(raw) as unknown;
      if (validate) {
        const v = validate(parsed);
        return v == null ? initial : v;
      }
      return parsed as T;
    } catch {
      return initial;
    }
  });

  const set = useCallback(
    (next: T | ((prev: T) => T)) => {
      setValue((prev) => {
        const resolved =
          typeof next === "function" ? (next as (p: T) => T)(prev) : next;
        try {
          if (resolved == null) localStorage.removeItem(key);
          else localStorage.setItem(key, JSON.stringify(resolved));
        } catch {
          /* private mode / quota — keep in-memory value */
        }
        return resolved;
      });
    },
    [key],
  );

  return [value, set];
}

/** Tracks the user's reduced-motion preference (live). */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });
  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const on = () => setReduced(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return reduced;
}
