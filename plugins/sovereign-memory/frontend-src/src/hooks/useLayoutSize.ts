import { useCallback, useState } from "react";

const LS_KEY = "sm.layout.v1";

type LayoutBag = Record<string, number | undefined>;

function loadLayout(): LayoutBag {
  try {
    return JSON.parse(localStorage.getItem(LS_KEY) || "{}") as LayoutBag;
  } catch {
    return {};
  }
}

function saveLayout(patch: LayoutBag): LayoutBag {
  const cur = loadLayout();
  const next = { ...cur, ...patch };
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(next));
  } catch {
    /* private mode etc. */
  }
  return next;
}

export function useLayoutSize(
  key: string,
  defaultSize: number,
): [number, (v: number | null) => void, () => void] {
  const [size, setSize] = useState<number>(() => {
    const stored = loadLayout()[key];
    return typeof stored === "number" ? stored : defaultSize;
  });
  const persist = useCallback(
    (v: number | null) => {
      const next = v == null ? defaultSize : v;
      setSize(next);
      saveLayout({ [key]: next });
    },
    [key, defaultSize],
  );
  const reset = useCallback(() => {
    setSize(defaultSize);
    const cur = loadLayout();
    delete cur[key];
    try {
      localStorage.setItem(LS_KEY, JSON.stringify(cur));
    } catch {
      /* noop */
    }
  }, [key, defaultSize]);
  return [size, persist, reset];
}

export function resetAllLayout(): void {
  try {
    localStorage.removeItem(LS_KEY);
  } catch {
    /* noop */
  }
  window.location.reload();
}
