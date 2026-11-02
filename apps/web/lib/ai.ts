"use client";

// Model-backed features. Every endpoint reports `enabled: false` when AI_ENABLED is off,
// and callers render nothing in that case.

import { type InfiniteData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchJson } from "./queries";

export type AiJobType = "ai.community_names" | "ai.reconnect" | "ai.digest" | "ai.person_weeks" | "ai.profiles" | "ai.post_media";

export interface ImageOCRStatus {
  enabled: boolean; include_chat: boolean; model: string; daily_limit: number; target_days: number; baseline_images: number;
  counts: Partial<Record<"pending" | "running" | "complete" | "failed", number>>;
  attempted_today: number; daily_remaining: number; estimated_days_remaining: number;
}

export interface AiStatus {
  image_ocr?: ImageOCRStatus;
  enabled: boolean;
  reachable?: boolean;
  models?: { batch: string; writer: string; person: string; vision: string };
  jobs?: { job_type: AiJobType; period?: string; status?: string | null; completed_at?: string | null; error?: string | null }[];
}

export interface Source { kind: "post" | "profile"; id: string }

export interface ReconnectPerson {
  profile_id: string;
  name: string;
  avatar_media_id: string | null;
  peak: number;
  last_contact_at: string;
  suggestion: { reason: string; opener: string; sources: Source[] } | null;
}

export type ReconnectAction = { state: "done" } | { state: "dismissed" } | { state: "snoozed"; snooze: "2w" | "1m" };

export interface DigestSummary { week_start: string; generated_at: string; summary: string | null; life_events: number }

export interface DigestPerson { profile_id: string; name: string; avatar_media_id?: string | null }

export interface Digest {
  week_start: string;
  generated_at: string;
  model: string | null;
  facts: {
    totals: { messages: number; people: number; posts: number };
    top_contacts: (DigestPerson & { messages: number; calls: number })[];
    new_people: DigestPerson[];
    new_links: (DigestPerson & { provider: string })[];
    upcoming_dates: (DigestPerson & { kind: "birthday" | "anniversary" | "other"; label?: string; date: string; days_until: number; years?: number })[];
    newly_dormant: (DigestPerson & { last_contact_at: string; messages: number })[];
  };
  narrative: {
    summary: string;
    life_events: { profile_id: string; kind: LifeEventKind; text: string; post_id: string }[];
    suggestions: { profile_id: string; reason: string }[];
  } | null;
  people: Record<string, { name: string; avatar_media_id: string | null }>;
  post_dates: Record<string, string>;
}

export type LifeEventKind = "new_job" | "move" | "travel" | "wedding" | "graduation" | "new_baby" | "other";

export type SuggestionKind = "birthday" | "residence" | "hometown" | "company" | "job" | "school" | "email" | "phone" | "food"
  | "interests" | "family" | "meetup";

/** A line the model cited; chat lines link back to the conversation at that moment. */
export interface Evidence {
  kind: "direct" | "group" | "post" | "profile";
  id: string;
  conversation_id?: string;
  at: string;
  text: string;
  speaker?: "我" | "TA";
  group?: string;
}

export interface ProfileSuggestion {
  fingerprint: string;
  kind: SuggestionKind;
  label: string;
  value: string;
  date: string | null;
  quote: string;
  confidence: "high" | "medium";
  evidence: Evidence[];
}

export interface ProfileAi {
  enabled: boolean;
  queued?: boolean;
  memory?: { summary: string; facts: { label: string; value: string }[]; gaps: string[]; generated_at: string } | null;
  weekly_updates?: PersonWeeklyUpdate[];
  suggestions?: ProfileSuggestion[];
}

export interface WeeklySource {
  kind: "post" | "story" | "direct" | "group" | "profile";
  id: string;
  at: string | null;
  discovered: boolean;
  conversation_id?: string | null;
  quote: string;
}

export interface PersonWeeklyUpdate {
  week_start: string;
  generated_at: string;
  summary: string;
  sources: WeeklySource[];
}

export interface WeeklyHighlight {
  profile_id: string;
  name: string;
  avatar_media_id: string | null;
  week_start: string;
  summary: string;
}

export interface SuggestionPerson { profile_id: string; name: string; avatar_media_id: string | null; generated_at: string; items: ProfileSuggestion[] }

export interface SuggestionPage {
  enabled: boolean;
  total: number;
  items: number;
  kinds: Partial<Record<SuggestionKind, number>>;
  people: SuggestionPerson[];
}

export type SuggestionDecision = { action: "accept"; value?: string; date?: string } | { action: "ignore" };

const keys = {
  status: ["ai", "status"] as const,
  reconnect: ["ai", "reconnect"] as const,
  digests: ["ai", "digests"] as const,
  digest: (week: string) => ["ai", "digest", week] as const,
  profile: (id: string) => ["ai", "profile", id] as const,
  weeklyHighlights: ["ai", "weekly-highlights"] as const,
  suggestions: ["ai", "suggestions"] as const,
};

export function useAiStatus() {
  return useQuery({ queryKey: keys.status, queryFn: () => fetchJson<AiStatus>("/api/v1/ai/status") });
}

export function useRunAiJob() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (job: AiJobType) => fetchJson<{ job_id: string }>(`/api/v1/ai/jobs/${job}`, { method: "POST" }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.status }),
  });
}

type ReconnectData = { enabled: boolean; people: ReconnectPerson[] };

/** A fresh weighted shuffle from the server on every visit; nothing is kept once the home page unmounts. */
export function useReconnect() {
  return useQuery({
    queryKey: keys.reconnect,
    queryFn: () => fetchJson<ReconnectData>("/api/v1/reconnect"),
    staleTime: 0,
    gcTime: 0,
  });
}

const removedAt = new Map<string, number>();

/** Acts on a suggestion, or undoes the last action with `null`. The card leaves the list immediately and an undo
 * puts it back where it was; the rest of the draw stays put, since refetching would reshuffle it. */
export function useReconnectAction() {
  const client = useQueryClient();
  const edit = (change: (people: ReconnectPerson[]) => ReconnectPerson[]) =>
    client.setQueryData<ReconnectData>(keys.reconnect, (data) => data && { ...data, people: change(data.people) });
  const remove = (person: ReconnectPerson) => edit((people) => {
    removedAt.set(person.profile_id, people.findIndex((other) => other.profile_id === person.profile_id));
    return people.filter((other) => other.profile_id !== person.profile_id);
  });
  const restore = (person: ReconnectPerson) => edit((people) => {
    const rest = people.filter((other) => other.profile_id !== person.profile_id);
    const at = Math.max(0, Math.min(removedAt.get(person.profile_id) ?? 0, rest.length));
    return [...rest.slice(0, at), person, ...rest.slice(at)];
  });
  return useMutation({
    mutationFn: ({ person, action }: { person: ReconnectPerson; action: ReconnectAction | null }) =>
      fetchJson(`/api/v1/reconnect/${person.profile_id}`, action
        ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(action) }
        : { method: "DELETE" }),
    onMutate: ({ person, action }) => (action ? remove(person) : restore(person)),
    onError: (_error, { person, action }) => (action ? restore(person) : remove(person)),
  });
}

export function useDigests() {
  return useQuery({
    queryKey: keys.digests,
    queryFn: () => fetchJson<{ enabled: boolean; digests: DigestSummary[] }>("/api/v1/digests"),
  });
}

export function useDigest(week: string | undefined) {
  return useQuery({
    queryKey: keys.digest(week ?? ""),
    queryFn: () => fetchJson<Digest>(`/api/v1/digests/${week}`),
    enabled: Boolean(week),
  });
}

export function useProfileAi(profileId: string) {
  return useQuery({
    queryKey: keys.profile(profileId),
    queryFn: () => fetchJson<ProfileAi>(`/api/v1/ai/profiles/${profileId}`),
    refetchInterval: (query) => (query.state.data?.queued ? 15_000 : false),
  });
}

export function useWeeklyHighlights() {
  return useQuery({
    queryKey: keys.weeklyHighlights,
    queryFn: () => fetchJson<{ enabled: boolean; week_start: string | null; people: WeeklyHighlight[] }>("/api/v1/ai/weekly-highlights"),
    staleTime: 0,
    gcTime: 0,
  });
}

export function useRefreshProfileAi(profileId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => fetchJson(`/api/v1/ai/profiles/${profileId}/refresh`, { method: "POST" }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.profile(profileId) }),
  });
}

export function useSuggestionPage(kind: SuggestionKind | null) {
  return useInfiniteQuery({
    queryKey: [...keys.suggestions, kind],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "20", offset: String(pageParam), ...(kind ? { kind } : {}) });
      return fetchJson<SuggestionPage>(`/api/v1/ai/suggestions?${params}`);
    },
    getNextPageParam: (page, _pages, offset) => page.people.length && offset + page.people.length < page.total
      ? offset + page.people.length : undefined,
  });
}

/** Accepts or ignores one suggestion, or with `null` undoes an ignore. Accepting writes the profile. */
export function useDecideSuggestion() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ profileId, fingerprint, decision }: { profileId: string; fingerprint: string; decision: SuggestionDecision | null }) =>
      fetchJson(`/api/v1/ai/suggestions/${profileId}/${fingerprint}`, decision
        ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(decision) }
        : { method: "DELETE" }),
    onMutate: ({ profileId, fingerprint, decision }) => {
      if (!decision) return;
      const without = (items: ProfileSuggestion[] = []) => items.filter((item) => item.fingerprint !== fingerprint);
      client.setQueryData<ProfileAi>(keys.profile(profileId), (data) => data && { ...data, suggestions: without(data.suggestions) });
      client.setQueriesData<InfiniteData<SuggestionPage>>({ queryKey: keys.suggestions }, (data) => data && ({
        ...data, pages: data.pages.map((page) => ({ ...page,
          people: page.people.map((person) => person.profile_id === profileId ? { ...person, items: without(person.items) } : person),
        })),
      }));
    },
    onSettled: (_result, _error, { profileId, decision }) => {
      void client.invalidateQueries({ queryKey: keys.profile(profileId) });
      void client.invalidateQueries({ queryKey: keys.suggestions });
      if (decision?.action === "accept") void client.invalidateQueries({ queryKey: ["profile", profileId] });
    },
  });
}

/** Saves a hand-typed community name, or with `null` returns to the model's or the default name. */
export function useRenameCommunity() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ key, label }: { key: string; label: string | null }) =>
      fetchJson<{ key: string; label?: string; label_source?: "manual" | "model" }>(`/api/v1/communities/${encodeURIComponent(key)}/name`, label
        ? { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label }) }
        : { method: "DELETE" }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["orbit"] });
      client.invalidateQueries({ queryKey: ["relationship-graph"] });
    },
  });
}


export function useUpdateImageOCR() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (data: { daily_limit?: number; enabled?: boolean }) => fetchJson<ImageOCRStatus>("/api/v1/ai/image-ocr", {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
    }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.status }),
  });
}

export interface WeeklyPeoplePage {
  enabled: boolean;
  week_start: string | null;
  weeks: string[];
  total: number;
  people: (WeeklyHighlight & PersonWeeklyUpdate)[];
  next_cursor: string | null;
}

export function useWeeklyPeople(week?: string) {
  return useInfiniteQuery({
    queryKey: ["ai", "weekly-people", week ?? "latest"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: "12" });
      if (week) params.set("week", week);
      if (pageParam) params.set("after", pageParam);
      return fetchJson<WeeklyPeoplePage>(`/api/v1/ai/weekly-people?${params}`);
    },
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
}
