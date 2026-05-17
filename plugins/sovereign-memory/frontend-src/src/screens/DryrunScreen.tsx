import { useState } from "react";
import { prepareOutcome, type PreparedOutcomePacket } from "../api";
import { Chip } from "../components/Chip";
import {
  ArchivalBand,
  PanelHeader,
  StateBanner,
} from "../components/atoms";

interface Props {
  layout: "columns" | "accordion" | "tray";
  outcome: PreparedOutcomePacket | null;
  setOutcome: (o: PreparedOutcomePacket | null) => void;
  defaultTask: string;
}

type DraftDecision = "approve" | "defer" | "reject" | undefined;

interface DraftRow {
  id: string;
  title: string;
  kind: "learn" | "log" | "dns" | "expires";
}

function rowsFor(
  outcome: PreparedOutcomePacket | null,
): { learn: DraftRow[]; log: DraftRow[]; dns: DraftRow[] } {
  if (!outcome)
    return { learn: [], log: [], dns: [] };
  const d = outcome.outcomeDraft;
  let n = 0;
  const mk = (title: string, kind: DraftRow["kind"]): DraftRow => ({
    id: `draft_${(++n).toString().padStart(3, "0")}_${kind}`,
    title,
    kind,
  });
  return {
    learn: d.learnCandidates.map((t) => mk(t, "learn")),
    log: [
      ...d.logOnly.map((t) => mk(t, "log")),
      ...d.expires.map((t) => mk(t, "expires")),
    ],
    dns: d.doNotStore.map((t) => mk(t, "dns")),
  };
}

export function DryrunScreen({ layout, outcome, setOutcome, defaultTask }: Props) {
  const [task, setTask] = useState(defaultTask);
  const [summary, setSummary] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Record<string, DraftDecision>>({});

  const apply = (id: string, action: DraftDecision) =>
    setDecisions((prev) => ({ ...prev, [id]: action }));

  const submit = async () => {
    if (!task.trim() || !summary.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await prepareOutcome({
        task: task.trim(),
        summary: summary.trim(),
      });
      setOutcome(result);
      setDecisions({});
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const { learn, log, dns } = rowsFor(outcome);
  const totalDrafts = learn.length + log.length + dns.length;

  const Card = ({ d }: { d: DraftRow }) => {
    const state = decisions[d.id];
    const bg =
      state === "approve"
        ? "var(--verdigris-soft)"
        : state === "reject"
          ? "var(--persimmon-soft)"
          : undefined;
    return (
      <div className="draft-card" style={bg ? { background: bg } : undefined}>
        <div className="draft-card-title">
          <span style={{ flex: 1 }}>{d.title}</span>
          <span className="draft-card-id">{d.id}</span>
        </div>
        <div className="draft-card-meta">
          <Chip kind="default">{d.kind}</Chip>
          {d.kind === "learn" && <Chip kind="learn">LEARN CANDIDATE</Chip>}
          {(d.kind === "log" || d.kind === "expires") && <Chip kind="log">LOG-ONLY</Chip>}
          {d.kind === "dns" && <Chip kind="dns">DO-NOT-STORE</Chip>}
        </div>
        <div className="draft-card-actions">
          {d.kind === "learn" && (
            <>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => apply(d.id, "approve")}
              >
                Approve
              </button>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => apply(d.id, "defer")}
              >
                Defer
              </button>
              <button
                type="button"
                className="btn btn-danger btn-sm"
                onClick={() => apply(d.id, "reject")}
              >
                Reject
              </button>
            </>
          )}
          {(d.kind === "log" || d.kind === "expires") && (
            <>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => apply(d.id, "approve")}
              >
                Promote to learn
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => apply(d.id, "defer")}
              >
                Acknowledge
              </button>
            </>
          )}
          {d.kind === "dns" && (
            <>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => apply(d.id, "defer")}
              >
                Inspect redaction
              </button>
              <button
                type="button"
                className="btn btn-danger btn-sm"
                onClick={() => apply(d.id, "reject")}
              >
                Confirm do-not-store
              </button>
            </>
          )}
        </div>
      </div>
    );
  };

  const Col = ({
    title,
    items,
    badge,
    bannerClass,
    banner,
  }: {
    title: string;
    items: DraftRow[];
    badge?: React.ReactNode;
    bannerClass: string;
    banner: string;
  }) => (
    <div className="panel">
      <PanelHeader title={title} sub={`${items.length} drafts`} actions={badge} />
      <div className={`dryrun-col-banner ${bannerClass}`}>{banner}</div>
      <div>
        {items.length === 0 ? (
          <div style={{ padding: 16 }} className="muted mono">
            None.
          </div>
        ) : (
          items.map((d) => <Card key={d.id} d={d} />)
        )}
      </div>
    </div>
  );

  return (
    <>
      <ArchivalBand
        eyebrow="DRY-RUN REVIEW · AFM LOOP · NO STATE WAS WRITTEN"
        title={outcome ? `Compile pass — ${outcome.task}` : "Dry-run review"}
        meta={[
          { k: "DRAFTS", v: outcome ? String(totalDrafts) : "—" },
          { k: "MODE", v: outcome?.mode || "—" },
          { k: "PROFILE", v: outcome?.profile || "—" },
          { k: "AFM", v: outcome?.afm.used ? "used" : "off" },
        ]}
      />

      <div
        className="panel-muted"
        style={{
          padding: 12,
          display: "flex",
          gap: 14,
          alignItems: "center",
          borderRadius: 2,
          flexWrap: "wrap",
        }}
      >
        <Chip kind="afm">AFM-SAFE · DRY-RUN</Chip>
        <span style={{ fontSize: 13, flex: 1, minWidth: 220 }}>
          The AFM loop produced these drafts as <b>review surfaces</b>. Nothing is in the
          vault, the SQLite truth, or any FAISS shard until you approve. Rejection is
          reversible.
        </span>
      </div>

      <div className="panel">
        <PanelHeader title="Submit outcome" sub="POST /api/prepare-outcome" />
        <div className="panel-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <label className="mono muted" style={{ fontSize: 10, letterSpacing: "0.12em", textTransform: "uppercase" }}>
            Task
            <input
              className="input"
              style={{ marginTop: 4 }}
              value={task}
              onChange={(e) => setTask(e.target.value)}
              placeholder="What was the task?"
            />
          </label>
          <label className="mono muted" style={{ fontSize: 10, letterSpacing: "0.12em", textTransform: "uppercase" }}>
            Summary
            <textarea
              className="input"
              style={{ marginTop: 4, height: 96, resize: "vertical", fontFamily: "var(--font-ui)", fontSize: 13.5 }}
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              placeholder="What changed? What did you verify?"
            />
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              className="btn btn-primary"
              onClick={submit}
              disabled={loading || !task.trim() || !summary.trim()}
            >
              {loading ? "Compiling…" : "Run dry-run"}
            </button>
          </div>
          {error && <StateBanner state="error">prepare-outcome failed: {error}</StateBanner>}
        </div>
      </div>

      {outcome ? (
        <div className="dryrun-grid" data-layout={layout}>
          <Col
            title="LEARN CANDIDATES"
            badge={<Chip kind="learn">REVERSIBLE</Chip>}
            bannerClass="dryrun-col-banner-safe"
            banner="Approving merges the draft into the agent's vault as a sourced wiki page."
            items={learn}
          />
          <Col
            title="LOG-ONLY"
            badge={<Chip kind="log">OBSERVE</Chip>}
            bannerClass="dryrun-col-banner-log"
            banner="Insufficient evidence to act. Promote only when a second signal arrives."
            items={log}
          />
          <Col
            title="DO-NOT-STORE"
            badge={<Chip kind="dns">EXCLUDED</Chip>}
            bannerClass="dryrun-col-banner-dns"
            banner="Drafts referenced private or out-of-scope material. Confirm to keep them out."
            items={dns}
          />
        </div>
      ) : (
        <StateBanner state="empty">
          Submit a task + summary above to see the AFM dry-run partition.
        </StateBanner>
      )}
    </>
  );
}
