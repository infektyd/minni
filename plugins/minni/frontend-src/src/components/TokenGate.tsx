import { useState } from "react";
import { setConsoleToken } from "../api";

// Shown when the API rejects requests with console_auth_required: the static
// shell is public but every /api route needs the bearer token. The operator
// finds it in the ui-server startup log (or set MINNI_CONSOLE_TOKEN), or opens
// the ?token= URL the server prints, which bypasses this gate entirely.
export function TokenGate({ onSubmit }: { onSubmit: () => void }) {
  const [value, setValue] = useState("");

  return (
    <div className="token-gate">
      <form
        className="token-gate-panel"
        onSubmit={(e) => {
          e.preventDefault();
          const token = value.trim();
          if (!token) return;
          setConsoleToken(token);
          onSubmit();
        }}
      >
        <div className="token-gate-title">
          <span className="rune">⬢</span> minni · console token
        </div>
        <p className="token-gate-hint">
          This console's API is locked. Paste the token from the ui-server
          startup log (or your MINNI_CONSOLE_TOKEN), or reopen the console via
          the <code>?token=…</code> link the server printed.
        </p>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="console token"
          aria-label="Console token"
        />
        <button className="btn btn-primary" type="submit" disabled={!value.trim()}>
          Connect
        </button>
      </form>
    </div>
  );
}
