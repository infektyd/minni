// Minni Memory Board — staged learnings data hook
//
// This module wraps the pure data mappers from boardData with a React hook
// that manages fetching, resolving, and fallback to sample data.

import { useState, useEffect, useCallback } from "react";
import { useMountedRef } from "./boardHooks";
import { AuthRequiredError, listCandidates, resolveCandidate } from "../api";
import {
  type BoardLearning,
  SAMPLE_LEARNINGS,
  mapCandidates,
} from "./boardData";

export interface StagedLearningsState {
  learnings: BoardLearning[];
  isLive: boolean;
  loading: boolean;
  error: string | null;
  resolve: (id: string, decision: "accepted" | "rejected") => Promise<void>;
  refresh: () => Promise<void>;
}

/**
 * Custom React hook managing fetching, resolving, and fallback to sample data.
 * On API success (even with 0 candidates), zone is LIVE; on error, falls back to SAMPLE_LEARNINGS.
 */
export function useStagedLearnings(refreshTrigger?: number): StagedLearningsState {
  const [learnings, setLearnings] = useState<BoardLearning[]>(SAMPLE_LEARNINGS);
  const [isLive, setIsLive] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isMounted = useMountedRef();

  const fetchLearnings = useCallback(async () => {
    setLoading(true);
    try {
      const response = await listCandidates(200, "proposed");
      if (isMounted.current) {
        // listCandidates returns unwrapped data or throws on error
        const mapped = mapCandidates(response.candidates || []);
        setLearnings(mapped);
        setIsLive(true);
        setError(null);
      }
    } catch (err: any) {
      if (isMounted.current) {
        setLearnings(SAMPLE_LEARNINGS);
        setIsLive(false);
        // Distinguish AuthRequiredError from daemon errors
        const isAuthError = err instanceof AuthRequiredError;
        if (isAuthError) {
          setError("Enter console token");
        } else {
          setError(`Daemon error: ${err.message || String(err)}`);
        }
      }
    } finally {
      if (isMounted.current) {
        setLoading(false);
      }
    }
  }, [isMounted]);

  useEffect(() => {
    fetchLearnings();
  }, [fetchLearnings, refreshTrigger]);

  const resolve = useCallback(async (id: string, decision: "accepted" | "rejected") => {
    if (!isLive) {
      setLearnings((prev) => prev.filter((item) => item.id !== id));
      return;
    }

    const candidateId = id.startsWith("C-") ? id.slice(2) : id;
    try {
      // Map UI 'accepted'/'rejected' to daemon 'accept'/'reject' (DEFECT 4)
      const daemonDecision = decision === 'accepted' ? 'accept' : 'reject';
      await resolveCandidate(candidateId, daemonDecision);
      await fetchLearnings();
    } catch (err: any) {
      if (isMounted.current) {
        setError(err.message || "Failed to resolve candidate");
      }
      throw err;
    }
  }, [isLive, fetchLearnings, isMounted]);

  return {
    learnings,
    isLive,
    loading,
    error,
    resolve,
    refresh: fetchLearnings,
  };
}
