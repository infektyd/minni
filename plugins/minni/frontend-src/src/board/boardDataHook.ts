// Minni Memory Board — zone data hooks
//
// One hook per zone. Sample fallback is gone: success (including empty) → isLive;
// non-auth failure → error + empty data so the board can render OFFLINE.
// AuthRequiredError is reported via onAuthRequired so App can raise TokenGate;
// the zone still goes offline for that paint.

import { useState, useEffect, useCallback, useRef } from "react";
import { useMountedRef } from "./boardHooks";
import {
  AuthRequiredError,
  listCandidates,
  resolveCandidate,
  getAgents,
  getLogOnly,
  getQuarantine,
  getRecallState,
  getHandoffs,
  getPolicy,
  type CandidateRow,
  type HandoffRow,
  type PolicyReport,
} from "../api";
import {
  type BoardLearning,
  type BoardLog,
  type BoardDeny,
  type BoardAgent,
  type BoardRecallResult,
  type AgentRow,
  type RecallStatePayload,
  mapCandidates,
  mapLogOnlyCandidates,
  mapQuarantineCandidates,
  mapAgents,
  mapRecallState,
  zoneFetchSuccess,
  zoneFetchFailure,
} from "./boardData";

// Re-export pure helpers for tests / consumers
export { zoneFetchSuccess, zoneFetchFailure };

function errorMessage(err: unknown): string {
  if (err instanceof AuthRequiredError) return "Enter console token";
  if (err instanceof Error) return err.message || String(err);
  return String(err);
}

function isAuthErr(err: unknown): boolean {
  return err instanceof AuthRequiredError;
}

// ── STAGED ──────────────────────────────────────────────────────────────────

export interface StagedLearningsState {
  /** Plan alias for learnings (plan shape: { data, isLive, error }). */
  data: BoardLearning[];
  learnings: BoardLearning[];
  isLive: boolean;
  loading: boolean;
  error: string | null;
  resolve: (id: string, decision: "accepted" | "rejected") => Promise<void>;
  refresh: () => Promise<void>;
}

/**
 * Fetch proposed candidates. Live empty is valid; error → OFFLINE (no samples).
 * AuthRequiredError notifies onAuthRequired so the console TokenGate can open.
 */
export function useStagedLearnings(
  refreshTrigger?: number,
  onAuthRequired?: () => void,
): StagedLearningsState {
  const [learnings, setLearnings] = useState<BoardLearning[]>([]);
  const [isLive, setIsLive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const isMounted = useMountedRef();
  const authCb = useRef(onAuthRequired);
  authCb.current = onAuthRequired;

  const fetchLearnings = useCallback(async () => {
    setLoading(true);
    try {
      const response = await listCandidates(200, "proposed");
      if (isMounted.current) {
        const result = zoneFetchSuccess(mapCandidates(response.candidates || []));
        setLearnings(result.data);
        setIsLive(true);
        setError(null);
      }
    } catch (err: unknown) {
      if (isMounted.current) {
        const result = zoneFetchFailure<BoardLearning[]>([], err, isAuthErr);
        setLearnings(result.data);
        setIsLive(false);
        setError(result.error);
        if (result.kind === "error" && result.authRequired) authCb.current?.();
      }
    } finally {
      if (isMounted.current) setLoading(false);
    }
  }, [isMounted]);

  useEffect(() => {
    void fetchLearnings();
  }, [fetchLearnings, refreshTrigger]);

  const resolve = useCallback(
    async (id: string, decision: "accepted" | "rejected") => {
      if (!isLive) {
        setError("Cannot resolve while staged zone is offline");
        throw new Error("staged offline");
      }
      const candidateId = id.startsWith("C-") ? id.slice(2) : id;
      try {
        const daemonDecision = decision === "accepted" ? "accept" : "reject";
        await resolveCandidate(candidateId, daemonDecision);
        await fetchLearnings();
      } catch (err: unknown) {
        if (isMounted.current) {
          setError(errorMessage(err));
          if (err instanceof AuthRequiredError) authCb.current?.();
        }
        throw err;
      }
    },
    [isLive, fetchLearnings, isMounted],
  );

  return {
    data: learnings,
    learnings,
    isLive,
    loading,
    error,
    resolve,
    refresh: fetchLearnings,
  };
}

// ── Generic zone state shape ────────────────────────────────────────────────

export interface ZoneDataState<T> {
  data: T;
  isLive: boolean;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

function useZoneFetch<T>(
  initial: T,
  fetcher: () => Promise<T>,
  refreshTrigger?: number,
  pollMs?: number,
  onAuthRequired?: () => void,
): ZoneDataState<T> {
  const [data, setData] = useState<T>(initial);
  const [isLive, setIsLive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const isMounted = useMountedRef();
  const authCb = useRef(onAuthRequired);
  authCb.current = onAuthRequired;
  const initialRef = useRef(initial);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = await fetcher();
      if (isMounted.current) {
        const result = zoneFetchSuccess(next);
        setData(result.data);
        setIsLive(true);
        setError(null);
      }
    } catch (err: unknown) {
      if (isMounted.current) {
        const result = zoneFetchFailure(initialRef.current, err, isAuthErr);
        setData(result.data);
        setIsLive(false);
        setError(result.error);
        if (result.kind === "error" && result.authRequired) authCb.current?.();
      }
    } finally {
      if (isMounted.current) setLoading(false);
    }
  }, [fetcher, isMounted]);

  useEffect(() => {
    void refresh();
  }, [refresh, refreshTrigger]);

  useEffect(() => {
    if (!pollMs) return;
    const id = window.setInterval(() => {
      void refresh();
    }, pollMs);
    return () => window.clearInterval(id);
  }, [refresh, pollMs]);

  return { data, isLive, loading, error, refresh };
}

// ── RUNTIMES / agents ───────────────────────────────────────────────────────

export function useAgents(
  refreshTrigger?: number,
  onAuthRequired?: () => void,
): ZoneDataState<BoardAgent[]> {
  const fetcher = useCallback(async () => {
    const res = await getAgents();
    return mapAgents((res.agents || []) as AgentRow[]);
  }, []);
  return useZoneFetch<BoardAgent[]>([], fetcher, refreshTrigger, 8000, onAuthRequired);
}

// ── LOG-ONLY ────────────────────────────────────────────────────────────────

export function useLogOnly(
  refreshTrigger?: number,
  onAuthRequired?: () => void,
): ZoneDataState<BoardLog[]> {
  const fetcher = useCallback(async () => {
    const res = await getLogOnly(200);
    return mapLogOnlyCandidates((res.candidates || []) as CandidateRow[]);
  }, []);
  return useZoneFetch<BoardLog[]>([], fetcher, refreshTrigger, undefined, onAuthRequired);
}

// ── QUARANTINE ──────────────────────────────────────────────────────────────

export function useQuarantine(
  refreshTrigger?: number,
  onAuthRequired?: () => void,
): ZoneDataState<BoardDeny[]> {
  const fetcher = useCallback(async () => {
    const res = await getQuarantine(200);
    return mapQuarantineCandidates((res.candidates || []) as CandidateRow[]);
  }, []);
  return useZoneFetch<BoardDeny[]>([], fetcher, refreshTrigger, undefined, onAuthRequired);
}

// ── RECALL state ────────────────────────────────────────────────────────────

export interface RecallZoneData {
  results: BoardRecallResult[];
  query: string;
  present: boolean;
  message: string;
}

export function useRecallState(
  refreshTrigger?: number,
  onAuthRequired?: () => void,
): ZoneDataState<RecallZoneData> {
  const empty: RecallZoneData = {
    results: [],
    query: "",
    present: false,
    message: "no recent recall",
  };
  const fetcher = useCallback(async () => {
    const res = (await getRecallState()) as RecallStatePayload;
    return mapRecallState(res);
  }, []);
  return useZoneFetch<RecallZoneData>(empty, fetcher, refreshTrigger, undefined, onAuthRequired);
}

// ── HANDOFFS ────────────────────────────────────────────────────────────────

export function useHandoffs(
  refreshTrigger?: number,
  onAuthRequired?: () => void,
): ZoneDataState<HandoffRow[]> {
  const fetcher = useCallback(async () => {
    const res = await getHandoffs();
    return (res.handoffs || []) as HandoffRow[];
  }, []);
  return useZoneFetch<HandoffRow[]>([], fetcher, refreshTrigger, 8000, onAuthRequired);
}

// ── POLICY ──────────────────────────────────────────────────────────────────

export function usePolicy(
  refreshTrigger?: number,
  onAuthRequired?: () => void,
): ZoneDataState<PolicyReport | null> {
  const fetcher = useCallback(async () => {
    return (await getPolicy()) as PolicyReport;
  }, []);
  return useZoneFetch<PolicyReport | null>(null, fetcher, refreshTrigger, undefined, onAuthRequired);
}
