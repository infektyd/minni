import { useEffect, useState } from "react";
import {
  evidenceFromSource,
  prepareTask,
  type EvidenceClass,
  type EvidenceRow,
  type PreparedTaskPacket,
  type TaskProfile,
} from "../api";
import { AfmChip, AuthorityChip, PrivacyChip } from "../components/Chip";
import {
  ArchivalBand,
  Checkbox,
  FilterPill,
  PanelHeader,
  ScoreBar,
  StateBanner,
} from "../components/atoms";

interface Props {
  selected: Set<string>;
  setSelected: (next: Set<string>) => void;
  focusId: string | null;
  setFocusId: (id: string | null) => void;
  query: string;
  setQuery: (q: string) => void;
  profile: TaskProfile;
  setProfile: (p: TaskProfile) => void;
  packet: PreparedTaskPacket | null;
  setPacket: (p: PreparedTaskPacket | null) => void;
  setEvidence: (rows: EvidenceRow[]) => void;
  evidence: EvidenceRow[];
}

const CLS_OPTIONS: EvidenceClass[] = ["wiki", "raw", "log", "inbox", "code", "other"];

export function RecallScreen({
  selected,
  setSelected,
  focusId,
  setFocusId,
  query,
  setQuery,
  profile,
  setProfile,
  packet,
  setPacket,
  evidence,
  setEvidence,
}: Props) {
  const [filters, setFilters] = useState<Record<EvidenceClass, boolean>>({
    wiki: true,
    raw: true,
    log: true,
    inbox: true,
    code: true,
    other: true,
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runQuery = async (q: string) => {
    if (!q.trim()) {
      setEvidence([]);
      setPacket(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await prepareTask({ task: q.trim(), profile, includeVault: true });
      setPacket(result);
      const rows = (result.relevantSources || []).map((src) =>
        evidenceFromSource(src, result.outcomeDraft),
      );
      setEvidence(rows);
      const autoSelect = new Set(rows.filter((r) => r.selected).map((r) => r.id));
      setSelected(autoSelect);
      if (rows.length > 0 && !rows.some((r) => r.id === focusId)) {
        setFocusId(rows[0]!.id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (query) void runQuery(query);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filteredRows = evidence.filter((r) => filters[r.cls]);
  const counts: Record<EvidenceClass, number> = {
    wiki: 0,
    raw: 0,
    log: 0,
    inbox: 0,
    code: 0,
    other: 0,
  };
  for (const r of evidence) counts[r.cls]++;

  const incl = filteredRows.filter((r) => selected.has(r.id)).length;
  const budgetTokens = packet?.budget?.tokens ?? 0;
  const usedTokens = packet?.budgetTokens ?? 0;

  return (
    <>
      <ArchivalBand
        eyebrow={`WORKSPACE · ${packet?.mode?.toUpperCase() || "DETERMINISTIC"}`}
        title={query ? `Recall — ${query}` : "Recall"}
        meta={[
          { k: "PROFILE", v: profile },
          { k: "BUDGET", v: budgetTokens ? `${usedTokens.toLocaleString()} / ${budgetTokens.toLocaleString()} tok` : "—" },
          { k: "DAEMON", v: packet?.recall?.daemonOk ? "ok" : packet?.recall?.error || "—" },
          { k: "AFM", v: packet?.afm?.used ? "used" : packet?.afm?.requested ? "requested · skipped" : "off" },
        ]}
      />

      <div className="panel">
        <div className="panel-body" style={{ paddingBottom: 10 }}>
          <form
            className="recall-bar"
            onSubmit={(e) => {
              e.preventDefault();
              void runQuery(query);
            }}
          >
            <div className="recall-search">
              <div className="recall-search-prefix">
                <span style={{ width: 8, height: 8, background: "var(--verdigris)" }} />
                sovereign_recall
              </div>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="query the local memory spine…"
                spellCheck={false}
                aria-label="Recall query"
              />
              <div className="recall-search-meta">
                <span>{evidence.length} hits</span>
                <span>·</span>
                <span>profile {profile}</span>
              </div>
            </div>
            <select
              className="input"
              style={{ width: 130 }}
              value={profile}
              onChange={(e) => setProfile(e.target.value as TaskProfile)}
              aria-label="Budget profile"
            >
              <option value="compact">compact</option>
              <option value="standard">standard</option>
              <option value="deep">deep</option>
            </select>
            <button className="btn btn-primary" type="submit" disabled={loading}>
              {loading ? "Recalling…" : "Recall"}
            </button>
          </form>

          <div className="filter-row" style={{ marginTop: 12 }}>
            <span
              className="mono muted"
              style={{
                fontSize: 10,
                letterSpacing: "0.12em",
                textTransform: "uppercase",
                marginRight: 4,
              }}
            >
              SOURCE CLASS
            </span>
            {CLS_OPTIONS.map((cls) => (
              <FilterPill
                key={cls}
                on={filters[cls]}
                label={cls}
                count={counts[cls]}
                onClick={() => setFilters({ ...filters, [cls]: !filters[cls] })}
              />
            ))}
          </div>
        </div>

        <PanelHeader
          title={`Evidence · ${filteredRows.length} ranked`}
          sub={`${incl} included · ${Math.max(0, filteredRows.length - incl)} held back`}
        />
        <div className="panel-body--flush">
          {loading && <StateBanner state="loading">Querying daemon…</StateBanner>}
          {!loading && error && (
            <StateBanner state="error">prepare-task failed: {error}</StateBanner>
          )}
          {!loading && !error && evidence.length === 0 && (
            <StateBanner state="empty">
              No evidence yet. Type a query and press Recall.
            </StateBanner>
          )}
          {!loading && !error && filteredRows.length > 0 && (
            <div className="ledger">
              <div className="ledger-head">
                <div></div>
                <div>Score</div>
                <div>Source</div>
                <div>Class</div>
                <div>Inclusion reason</div>
                <div style={{ justifyContent: "flex-end", display: "flex" }}>
                  Privacy · Authority · AFM
                </div>
                <div style={{ justifyContent: "flex-end", display: "flex" }}>Action</div>
              </div>
              {filteredRows.map((e) => {
                const isSel = selected.has(e.id);
                const isFocus = focusId === e.id;
                return (
                  <div
                    key={e.id}
                    role="button"
                    tabIndex={0}
                    className={`ledger-row ${isFocus ? "is-selected" : ""} ${
                      e.private ? "is-private" : ""
                    }`}
                    onClick={() => setFocusId(e.id)}
                    onKeyDown={(ev) => {
                      if (ev.key === "Enter") setFocusId(e.id);
                    }}
                  >
                    <div>
                      <Checkbox
                        on={isSel}
                        onChange={(v) => {
                          const n = new Set(selected);
                          if (v) n.add(e.id);
                          else n.delete(e.id);
                          setSelected(n);
                        }}
                      />
                    </div>
                    <div className="ledger-cell-score">
                      <ScoreBar value={e.score} />
                    </div>
                    <div className="ledger-cell-source">
                      <span className="ledger-source-title">{e.title}</span>
                      <span className="ledger-source-path">{e.path}</span>
                    </div>
                    <div className="ledger-class">{e.cls}</div>
                    <div className="ledger-reason">{e.reason}</div>
                    <div className="ledger-cell-chips">
                      <PrivacyChip value={e.privacy} />
                      <AuthorityChip value={e.authority} />
                      <AfmChip value={e.afm} />
                    </div>
                    <div className="ledger-cell-action">
                      {isSel ? (
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={(ev) => {
                            ev.stopPropagation();
                            const n = new Set(selected);
                            n.delete(e.id);
                            setSelected(n);
                          }}
                        >
                          Exclude
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="btn btn-primary btn-sm"
                          onClick={(ev) => {
                            ev.stopPropagation();
                            const n = new Set(selected);
                            n.add(e.id);
                            setSelected(n);
                          }}
                        >
                          Include
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
