import { useRef } from "react";

interface Props {
  axis?: "x" | "y";
  side?: "right" | "left" | "top" | "bottom";
  value: number;
  onChange: (next: number | null) => void;
  min?: number;
  max?: number;
  className?: string;
}

export function ResizeHandle({
  axis = "x",
  side = "right",
  value,
  onChange,
  min = 120,
  max = 800,
  className = "",
}: Props) {
  const startRef = useRef<{ x: number; y: number; v: number } | null>(null);

  const onDown = (e: React.MouseEvent<HTMLDivElement>) => {
    e.preventDefault();
    startRef.current = { x: e.clientX, y: e.clientY, v: value };
    const onMove = (ev: MouseEvent) => {
      if (!startRef.current) return;
      const dx = ev.clientX - startRef.current.x;
      const dy = ev.clientY - startRef.current.y;
      let next = startRef.current.v;
      if (axis === "x") next += side === "right" ? dx : -dx;
      else next += side === "bottom" ? dy : -dy;
      next = Math.max(min, Math.min(max, next));
      onChange(next);
    };
    const onUp = () => {
      startRef.current = null;
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = axis === "x" ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  return (
    <div
      className={`resize-handle resize-handle-${axis} ${className}`}
      data-side={side}
      onMouseDown={onDown}
      onDoubleClick={() => onChange(null)}
      role="separator"
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      tabIndex={-1}
    />
  );
}
