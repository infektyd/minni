import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

const PANEL_STYLE = `
.twk-panel{position:fixed;right:16px;bottom:16px;z-index:2147483646;width:280px;
  max-height:calc(100vh - 32px);display:flex;flex-direction:column;
  background:var(--panel);color:var(--ink);
  border:1px solid var(--border-strong);border-radius:6px;
  box-shadow:0 12px 40px rgba(0,0,0,.18);
  font:11.5px/1.4 var(--font-ui);overflow:hidden}
html[data-theme="phosphor"] .twk-panel{box-shadow:0 0 0 1px rgba(107,227,162,0.18),0 12px 40px rgba(0,0,0,.5)}
.twk-hd{display:flex;align-items:center;justify-content:space-between;
  padding:10px 8px 10px 14px;cursor:default;user-select:none;
  border-bottom:1px solid var(--border)}
.twk-hd b{font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;font-family:var(--font-mono)}
.twk-x{appearance:none;border:0;background:transparent;color:var(--on-muted);
  width:22px;height:22px;border-radius:4px;cursor:pointer;font-size:14px;line-height:1}
.twk-x:hover{background:var(--hover);color:var(--ink)}
.twk-body{padding:10px 14px 14px;display:flex;flex-direction:column;gap:12px;
  overflow-y:auto;overflow-x:hidden;min-height:0}
.twk-row{display:flex;flex-direction:column;gap:6px}
.twk-row-h{flex-direction:row;align-items:center;justify-content:space-between;gap:10px}
.twk-lbl{display:flex;justify-content:space-between;align-items:baseline;
  color:var(--on-muted);font-family:var(--font-mono);font-size:10.5px;
  letter-spacing:.06em;text-transform:uppercase}
.twk-sect{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  color:var(--on-muted);padding:6px 0 0;border-top:1px solid var(--border);font-family:var(--font-mono)}
.twk-sect:first-child{border-top:0;padding-top:0}
.twk-seg{position:relative;display:flex;padding:2px;border-radius:4px;
  background:var(--panel-muted);user-select:none;border:1px solid var(--border)}
.twk-seg button{appearance:none;flex:1;border:0;background:transparent;color:inherit;
  font:inherit;font-weight:500;min-height:24px;border-radius:3px;cursor:pointer;
  padding:4px 6px;line-height:1.2;font-family:var(--font-mono);font-size:11px}
.twk-seg button[aria-checked="true"]{background:var(--ink);color:var(--on-ink)}
html[data-theme="phosphor"] .twk-seg button[aria-checked="true"]{background:var(--verdigris);color:var(--ink)}
.twk-btn{appearance:none;height:28px;padding:0 12px;border:1px solid var(--border-strong);
  border-radius:3px;background:var(--panel);color:var(--ink);font:inherit;
  font-family:var(--font-mono);font-size:11px;cursor:pointer}
.twk-btn:hover{background:var(--hover)}
.twk-toggle-button{position:fixed;right:16px;bottom:16px;z-index:2147483645;
  appearance:none;width:36px;height:36px;border-radius:50%;
  border:1px solid var(--border-strong);background:var(--panel);
  color:var(--ink);cursor:pointer;font-family:var(--font-mono);font-size:14px;
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 4px 12px rgba(0,0,0,.12)}
.twk-toggle-button:hover{background:var(--hover)}
html[data-theme="phosphor"] .twk-toggle-button{color:var(--verdigris);
  box-shadow:0 0 12px rgba(107,227,162,0.2)}
`;

export function TweaksPanel({
  title = "Tweaks",
  children,
}: {
  title?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const styleInjected = useRef(false);

  useEffect(() => {
    if (styleInjected.current) return;
    styleInjected.current = true;
    const el = document.createElement("style");
    el.textContent = PANEL_STYLE;
    document.head.appendChild(el);
    return () => {
      el.remove();
      styleInjected.current = false;
    };
  }, []);

  return (
    <div data-tweaks-host>
      {!open && (
        <button
          className="twk-toggle-button"
          aria-label="Open tweaks panel"
          onClick={() => setOpen(true)}
          type="button"
        >
          ⚙
        </button>
      )}
      {open && (
        <div className="twk-panel" role="dialog" aria-label="Tweaks">
          <div className="twk-hd">
            <b>{title}</b>
            <button
              className="twk-x"
              aria-label="Close tweaks"
              onClick={() => setOpen(false)}
              type="button"
            >
              ✕
            </button>
          </div>
          <div className="twk-body">{children}</div>
        </div>
      )}
    </div>
  );
}

export function TweakSection({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <>
      <div className="twk-sect">{label}</div>
      {children}
    </>
  );
}

export function TweakRadio<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="twk-row">
      <div className="twk-lbl">
        <span>{label}</span>
      </div>
      <div className="twk-seg" role="radiogroup" aria-label={label}>
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={o.value === value}
            onClick={() => onChange(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

export function TweakButton({
  onClick,
  children,
}: {
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button type="button" className="twk-btn" onClick={onClick}>
      {children}
    </button>
  );
}
