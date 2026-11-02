"use client";

import {useCallback, useEffect, useState} from "react";
import type {StagedItem} from "./types";

const STORAGE_KEY = "monica.linking.staged.v1";

/** Staged decisions keyed by source account, kept across reloads until applied. */
export function useStaged() {
  const [staged, setStaged] = useState<Record<string, StagedItem>>({});
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved) setStaged(JSON.parse(saved) as Record<string, StagedItem>);
    setLoaded(true);
  }, []);

  useEffect(() => {
    if (loaded) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(staged));
  }, [staged, loaded]);

  const stage = useCallback((items: StagedItem[]) => {
    setStaged((current) => {
      const next = {...current};
      for (const item of items) {
        // Confirming both directions of a pair would swap the two accounts' profiles.
        if (item.action === "confirm") for (const other of Object.values(next)) {
          if (other.action === "confirm" && other.expected_profile_id === item.profile_id && other.profile_id === item.expected_profile_id) delete next[other.account_id];
        }
        next[item.account_id] = {...item, error: undefined};
      }
      return next;
    });
  }, []);

  const unstage = useCallback((accountIds: string[]) => {
    setStaged((current) => {
      const next = {...current};
      for (const id of accountIds) delete next[id];
      return next;
    });
  }, []);

  const markFailed = useCallback((failures: Record<string, string>) => {
    setStaged((current) => {
      const next = {...current};
      for (const [id, error] of Object.entries(failures)) if (next[id]) next[id] = {...next[id], error};
      return next;
    });
  }, []);

  return {staged, stage, unstage, markFailed, clear: useCallback(() => setStaged({}), [])};
}
