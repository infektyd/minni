import { useCallback, useEffect, useState } from "react";

export interface Tweaks {
  theme: "paper" | "phosphor";
  density: "comfortable" | "compact";
  inspector: "right" | "bottom" | "overlay";
  band: "paper" | "graphite";
  dryrunLayout: "columns" | "accordion" | "tray";
}

export const TWEAK_DEFAULTS: Tweaks = {
  theme: "paper",
  density: "comfortable",
  inspector: "right",
  band: "paper",
  dryrunLayout: "columns",
};

const LS_KEY = "sov-tweaks";

function loadTweaks(): Tweaks {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return TWEAK_DEFAULTS;
    const parsed = JSON.parse(raw) as Partial<Tweaks>;
    return { ...TWEAK_DEFAULTS, ...parsed };
  } catch {
    return TWEAK_DEFAULTS;
  }
}

export function useTweaks(): [Tweaks, <K extends keyof Tweaks>(k: K, v: Tweaks[K]) => void] {
  const [tweaks, setTweaks] = useState<Tweaks>(() => loadTweaks());
  const setTweak = useCallback(
    <K extends keyof Tweaks>(k: K, v: Tweaks[K]) => {
      setTweaks((prev) => {
        const next = { ...prev, [k]: v };
        try {
          localStorage.setItem(LS_KEY, JSON.stringify(next));
        } catch {
          /* noop */
        }
        return next;
      });
    },
    [],
  );

  // Reflect theme into <html data-theme=...> for CSS theme tokens.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", tweaks.theme);
    document.documentElement.setAttribute(
      "data-theme-layout",
      tweaks.theme === "phosphor" ? "operator" : "default",
    );
  }, [tweaks.theme]);

  return [tweaks, setTweak];
}
