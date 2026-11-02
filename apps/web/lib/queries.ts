"use client";

// Central data layer: every page reads through these react-query hooks so
// responses are shared, deduplicated and served stale-while-revalidate. The
// API's ETags make background revalidation cheap (304 without a body).

import {
  QueryClient,
  QueryClientProvider,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { createElement, type ReactNode, useState } from "react";

import { apiFetch } from "./api";
import type { ConversationItem, HomeSummary, InsightsSummary, ProfileOverview, SearchHit, SignificantDate, TimelineItem } from "./api-types";
import { clearDirectory, directoryKey, patchDirectoryProfile, patchDirectoryTag } from "./directory";

export class ApiError extends Error {
  constructor(readonly status: number, readonly path: string) {
    super(`${path} failed with ${status}`);
  }
}

/**
 * react-query decides when data is stale, so the browser must not also reuse a response on its own:
 * `no-cache` revalidates with the ETag (a 304 when unchanged) and a refetch after a write sees the write.
 */
export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, { cache: "no-cache", ...init });
  if (!response.ok) throw new ApiError(response.status, path);
  return (response.status === 204 ? undefined : await response.json()) as T;
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const queryKeys = {
  home: ["home"] as const,
  insights: (days: number) => ["insights", days] as const,
  profile: (id: string) => ["profile", id] as const,
  overview: (id: string) => ["profile", id, "overview"] as const,
  profileTab: (id: string, tab: ProfileTab, params?: object) => ["profile", id, tab, params ?? {}] as const,
  timeline: (filters: TimelineFilters) => ["timeline", filters] as const,
  conversations: (q: string) => ["conversations", q] as const,
  messages: (conversationId: string, params?: object) => ["conversation", conversationId, "messages", params ?? {}] as const,
  tags: (type?: string) => ["tags", type ?? "all"] as const,
  search: (q: string) => ["search", q] as const,
};

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 60_000,
        gcTime: 10 * 60_000,
        refetchOnWindowFocus: false,
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      },
    },
  });
}

/** Mount once in app/layout.tsx around the application shell. */
export function QueryProvider({ children }: { children: ReactNode }) {
  const [client] = useState(createQueryClient);
  return createElement(QueryClientProvider, { client }, children);
}

// ---- Page aggregates -------------------------------------------------------

export function useHome() {
  return useQuery({ queryKey: queryKeys.home, queryFn: () => fetchJson<HomeSummary>("/api/v1/home") });
}

export function useInsights(dormantDays: 90 | 180 | 365 = 90) {
  return useQuery({
    queryKey: queryKeys.insights(dormantDays),
    queryFn: () => fetchJson<InsightsSummary>(`/api/v1/insights?dormant_days=${dormantDays}`),
  });
}

export function useProfileOverview(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.overview(id ?? ""),
    queryFn: () => fetchJson<ProfileOverview>(`/api/v1/profiles/${id}/overview`),
    enabled: !!id,
  });
}

/** Full profile detail (fields, identities, facts) for the edit/detail tabs. */
export function useProfile(id: string | undefined, enabled = true) {
  return useQuery({
    queryKey: queryKeys.profile(id ?? ""),
    queryFn: ({ signal }) => fetchJson<Record<string, unknown>>(`/api/v1/profiles/${id}`, { signal }),
    enabled: !!id && enabled,
  });
}

const PROFILE_TABS = {
  history: "history",
  "structured-history": "structured-history",
  metrics: "metrics",
  locations: "locations",
  relationships: "relationships",
  photos: "photos",
  "chat-months": "chat-months",
  ribbon: "ribbon",
  "contact-events-summary": "contact-events/summary",
  "contact-events": "contact-events",
  "moments-circle": "moments-circle",
  messages: "messages",
} as const;

export type ProfileTab = keyof typeof PROFILE_TABS | "timeline-summary" | "social";

function profileTabPath(id: string, tab: ProfileTab, params: Record<string, string | number | undefined> = {}) {
  if (tab === "timeline-summary") return `/api/v1/timeline/summary${query({ profile_id: id })}`;
  if (tab === "social") return `/api/v1/metrics/social${query({ profile_id: id })}`;
  return `/api/v1/profiles/${id}/${PROFILE_TABS[tab]}${query(params)}`;
}

/** Detailed tab data; each tab is its own cached query, fetched only when shown. */
export function useProfileTab<T = unknown>(id: string | undefined, tab: ProfileTab,
                                           params?: Record<string, string | number | undefined>, enabled = true) {
  return useQuery({
    queryKey: queryKeys.profileTab(id ?? "", tab, params),
    queryFn: () => fetchJson<T>(profileTabPath(id!, tab, params)),
    enabled: !!id && enabled,
  });
}

// ---- Paginated feeds --------------------------------------------------------

export interface TimelineFilters {
  profileId?: string;
  provider?: string;
  media?: "ready";
  limit?: number;
}

export function useTimeline(filters: TimelineFilters = {}) {
  const limit = filters.limit ?? 18;
  return useInfiniteQuery({
    queryKey: queryKeys.timeline(filters),
    queryFn: ({ pageParam }) => fetchJson<TimelineItem[]>(`/api/v1/timeline${query({
      profile_id: filters.profileId, provider: filters.provider, media: filters.media, limit, cursor: pageParam,
    })}`),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => (page.length === limit ? page[page.length - 1].cursor : undefined),
  });
}

export function useTimelineEvent<T = unknown>(eventId: string | undefined) {
  return useQuery({
    queryKey: ["timeline-event", eventId ?? ""],
    queryFn: () => fetchJson<T>(`/api/v1/timeline/${encodeURIComponent(eventId!)}`),
    enabled: !!eventId,
    staleTime: 10 * 60_000,
  });
}

export function useConversations(q = "", limit = 40) {
  return useInfiniteQuery({
    queryKey: queryKeys.conversations(q),
    queryFn: ({ pageParam }) => fetchJson<ConversationItem[]>(`/api/v1/conversations${query({ q, limit, cursor: pageParam })}`),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => (page.length === limit ? page[page.length - 1].cursor : undefined),
  });
}

export interface MessageParams { q?: string; month?: string; timezone?: string; limit?: number }

export function useConversationMessages<T extends { occurred_at: string; id: string }>(
  conversationId: string | undefined, params: MessageParams = {},
) {
  const limit = params.limit ?? 50;
  return useInfiniteQuery({
    queryKey: queryKeys.messages(conversationId ?? "", params),
    queryFn: ({ pageParam }) => fetchJson<T[]>(`/api/v1/conversations/${conversationId}/messages${query({
      ...params, limit, before: pageParam?.before, before_id: pageParam?.beforeId,
    })}`),
    initialPageParam: undefined as { before: string; beforeId: string } | undefined,
    getNextPageParam: (page) => {
      const last = page[page.length - 1];
      return page.length === limit && last ? { before: last.occurred_at, beforeId: last.id } : undefined;
    },
    enabled: !!conversationId,
  });
}

/** Records and profile summaries are searched on the server; names, tags and post text are searched locally. */
export function useServerSearch(q: string) {
  return useQuery({
    queryKey: queryKeys.search(q),
    queryFn: ({ signal }) => fetchJson<SearchHit[]>(`/api/v1/search${query({ q })}`, { signal }),
    enabled: q.trim().length >= 2,
  });
}

// ---- Mutations --------------------------------------------------------------

function invalidateProfile(client: ReturnType<typeof useQueryClient>, id: string) {
  void client.invalidateQueries({ queryKey: queryKeys.profile(id) });
  void client.invalidateQueries({ queryKey: queryKeys.home });
}

export function useUpdateProfileField(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (edit: { field_key: string; value: unknown }) =>
      fetchJson(`/api/v1/profiles/${id}/fields`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(edit),
      }),
    onSuccess: (_result, edit) => {
      const column = edit.field_key === "display_name" ? "name" : edit.field_key === "summary" ? "summary" : null;
      if (column) patchDirectoryProfile(client, id, { [column]: edit.value });
      invalidateProfile(client, id);
    },
  });
}

/** The API's error code (such as `birthday_exists`), for a message next to the form. */
export class DateRejected extends Error {
  constructor(readonly code: string) { super(code); }
}

async function dateRequest(path: string, init: RequestInit): Promise<SignificantDate | undefined> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new DateRejected(typeof body.detail === "string" ? body.detail : "");
  }
  return response.status === 204 ? undefined : await response.json();
}

/** Dates appear in the profile, the overview header and the insights lists. */
function invalidateDates(client: ReturnType<typeof useQueryClient>, id: string) {
  invalidateProfile(client, id);
  void client.invalidateQueries({ queryKey: queryKeys.overview(id) });
  void client.invalidateQueries({ queryKey: ["insights"] });
}

function monthDay(date: Pick<SignificantDate, "month" | "day">): string | null {
  return date.month && date.day ? `${String(date.month).padStart(2, "0")}-${String(date.day).padStart(2, "0")}` : null;
}

/** Creates (no `previous`) or replaces a date; the directory's birthday sort follows the saved birthday. */
export function useSaveDate(profileId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ previous, body }: { previous?: SignificantDate; body: Record<string, unknown> }) =>
      dateRequest(previous ? `/api/v1/profiles/${profileId}/dates/${previous.id}` : `/api/v1/profiles/${profileId}/dates`,
        { method: previous ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    onSuccess: (saved, { previous }) => {
      if (saved!.kind === "birthday" || previous?.kind === "birthday") {
        patchDirectoryProfile(client, profileId, { birthday_md: saved!.kind === "birthday" ? monthDay(saved!) : null });
      }
      invalidateDates(client, profileId);
    },
  });
}

export function useDeleteDate(profileId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (date: SignificantDate) => dateRequest(`/api/v1/profiles/${profileId}/dates/${date.id}`, { method: "DELETE" }),
    onSuccess: (_result, date) => {
      if (date.kind === "birthday") patchDirectoryProfile(client, profileId, { birthday_md: null });
      invalidateDates(client, profileId);
    },
  });
}

export interface TagRef { id: string; name: string }

export function useAttachTag(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (tag: TagRef) => fetchJson(`/api/v1/profiles/${id}/tags/${tag.id}`, { method: "POST" }),
    onSuccess: (_result, tag) => {
      patchDirectoryTag(client, id, { add: tag });
      void client.invalidateQueries({ queryKey: ["tags"] });
      invalidateProfile(client, id);
    },
  });
}

/** Removes a tag from a profile, including an imported one; imports will not add it back. */
export function useDetachTag(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (tag: TagRef) => fetchJson(`/api/v1/profiles/${id}/tags/${tag.id}`, { method: "DELETE" }),
    onSuccess: (_result, tag) => {
      patchDirectoryTag(client, id, { remove: tag.id });
      void client.invalidateQueries({ queryKey: ["tags"] });
      invalidateProfile(client, id);
    },
  });
}

/** Creates the tag if the name is new (an existing name returns that tag). */
export function useCreateTag() {
  return useMutation({
    mutationFn: (name: string) => fetchJson<TagRef>("/api/v1/tags", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
    }),
  });
}

export function useCreateRelationship(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (relationship: { to_profile_id: string; relationship_type: string; note?: string }) =>
      fetchJson(`/api/v1/profiles/${id}/relationships`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(relationship),
      }),
    onSuccess: (_result, relationship) => {
      invalidateProfile(client, id);
      invalidateProfile(client, relationship.to_profile_id);
    },
  });
}

/** Clears every cached response and the persisted directory on logout. */
export async function clearClientData(client: QueryClient) {
  await clearDirectory(client);
  client.clear();
}

export function useProfileSummary(id: string) {
  return useQuery({ queryKey: ["profile", id, "summary"],
    queryFn: ({ signal }) => fetchJson<{ profile: import("../components/profile/types").Profile; has_posts: boolean; overview: Pick<ProfileOverview, "messages" | "contact_events"> }>(`/api/v1/profiles/${id}/summary`, { signal }) });
}
