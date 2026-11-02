"use client";

// Client-side contact directory: one versioned snapshot kept in IndexedDB and
// revalidated with its ETag, so lists, filters, pickers and tag counts never
// query the server per keystroke. Message full-text search stays server-side.

import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, set } from "idb-keyval";
import { useEffect, useMemo } from "react";

import { apiFetch } from "./api";
import { searchPeople, searchPosts, resetSearchWorkers, directoryVersion } from "./search-client";
import type { RawDirectory } from "./api-types";

export interface DirectoryProfile {
  id: string;
  type: "person" | "group";
  name: string;
  summary: string | null;
  avatarMediaId: string | null;
  tagIds: string[];
  companyIds: string[];
  schoolIds: string[];
  aliases: string[];
  providers: string[];
  headline: string | null;
  city: string | null;
  birthdayMd: string | null;
  lastContactAt: string | null;
  directMessageCount: number | null;
  memberCount: number | null;
  conversationId: string | null;
  /** Names this person uses inside WeChat groups; searched, not shown as aliases. */
  groupNames: string[];
}

export interface Directory {
  profiles: DirectoryProfile[];
  byId: Map<string, DirectoryProfile>;
  tags: Map<string, string>;
  companies: Map<string, string>;
  schools: Map<string, string>;
}

export interface DirectoryFilters {
  type?: "person" | "group";
  /** A profile must carry every listed tag. */
  tagIds?: string[];
  companyId?: string;
  schoolId?: string;
  provider?: string;
}

export type DirectorySort = "relevance" | "name" | "last_contact" | "messages" | "birthday";

interface Snapshot<T> { etag: string | null; data: T }

export const directoryKey = ["directory"] as const;
export const directoryPostsKey = ["directory", "posts"] as const;
const STORE_KEY = "monica.directory.v1";
const POSTS_STORE_KEY = "monica.directory-posts.v1";
const STALE_MS = 5 * 60_000;

async function revalidate<T>(url: string, storeKey: string, previous?: Snapshot<T>): Promise<Snapshot<T>> {
  const cached = previous ?? (await get<Snapshot<T>>(storeKey));
  const response = await apiFetch(url, { headers: cached?.etag ? { "If-None-Match": cached.etag } : {} });
  if (response.status === 304 && cached) return cached;
  if (!response.ok) throw new Error(`${url} failed with ${response.status}`);
  const snapshot = { etag: response.headers.get("ETag"), data: (await response.json()) as T };
  await set(storeKey, snapshot);
  return snapshot;
}

function useStoredSnapshot<T>(key: readonly string[], url: string, storeKey: string, enabled = true) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: key,
    queryFn: () => revalidate<T>(url, storeKey, client.getQueryData<Snapshot<T>>(key)),
    staleTime: STALE_MS,
    enabled,
  });
  useEffect(() => {
    if (!enabled || client.getQueryData(key)) return;
    // Paint from the persisted snapshot while the network revalidates it.
    void get<Snapshot<T>>(storeKey).then((stored) => {
      if (stored && !client.getQueryData(key)) client.setQueryData(key, stored, { updatedAt: 0 });
    });
  }, [client, enabled, key, storeKey]);
  return query;
}

function decode(raw: RawDirectory): Directory {
  const column = Object.fromEntries(raw.columns.map((name, index) => [name, index]));
  const lookup = (list: [string, string][]) => (indices: unknown) =>
    (indices as number[]).map((index) => list[index][0]);
  const tagIds = lookup(raw.tags);
  const companyIds = lookup(raw.companies);
  const schoolIds = lookup(raw.schools);
  const profiles = raw.profiles.map((row): DirectoryProfile => ({
    id: row[column.id] as string,
    type: row[column.type] as "person" | "group",
    name: row[column.name] as string,
    summary: row[column.summary] as string | null,
    avatarMediaId: row[column.avatar_media_id] as string | null,
    tagIds: tagIds(row[column.tag_ids]),
    companyIds: companyIds(row[column.company_ids]),
    schoolIds: schoolIds(row[column.school_ids]),
    aliases: row[column.aliases] as string[],
    providers: row[column.providers] as string[],
    headline: row[column.headline] as string | null,
    city: row[column.city] as string | null,
    birthdayMd: row[column.birthday_md] as string | null,
    lastContactAt: row[column.last_contact_at] as string | null,
    directMessageCount: row[column.direct_message_count] as number | null,
    memberCount: row[column.member_count] as number | null,
    conversationId: row[column.conversation_id] as string | null,
    // Snapshots cached before this column existed decode without it until the next refresh.
    groupNames: (row[column.group_names] as string[] | undefined) ?? [],
  }));
  return {
    profiles,
    byId: new Map(profiles.map((profile) => [profile.id, profile])),
    tags: new Map(raw.tags),
    companies: new Map(raw.companies),
    schools: new Map(raw.schools),
  };
}

// Every component shares one decoded directory (and search index) per snapshot.
const decodedSnapshots = new WeakMap<Snapshot<RawDirectory>, Directory>();

function decoded(snapshot: Snapshot<RawDirectory>): Directory {
  let directory = decodedSnapshots.get(snapshot);
  if (!directory) {
    directory = decode(snapshot.data);
    decodedSnapshots.set(snapshot, directory);
  }
  return directory;
}

/** The decoded directory, painted from IndexedDB first and revalidated by ETag. */
export function useDirectory() {
  const query = useStoredSnapshot<RawDirectory>(directoryKey, "/api/v1/directory", STORE_KEY);
  const directory = query.data ? decoded(query.data) : undefined;
  return { ...query, directory };
}

/** The WeChat group name that matched a query when the person's own names did not, to explain the hit. */
export function matchedGroupName(profile: DirectoryProfile, text: string): string | undefined {
  const needle = text.trim().toLowerCase();
  if (!needle || [profile.name, ...profile.aliases].some((value) => value.toLowerCase().includes(needle))) return undefined;
  return profile.groupNames.find((value) => value.toLowerCase().includes(needle));
}

/** Contacts list: text query (names, aliases, WeChat group names, pinyin, initials), filters and sort, all local. */
export function useDirectorySearch(text: string, filters: DirectoryFilters = {}, sort: DirectorySort = "relevance") {
  const { directory, ...query } = useDirectory();
  const { type, tagIds, companyId, schoolId, provider } = filters;
  const tagKey = (tagIds ?? []).join(",");
  const search = useQuery({
    queryKey: ["local-search", directoryVersion(directory), text, type, tagKey, companyId, schoolId, provider, sort],
    queryFn: ({ signal }) => searchPeople(directory!, text, { type, tagIds: tagKey ? tagKey.split(",") : [], companyId, schoolId, provider }, sort, undefined, signal),
    enabled: !!directory,
    gcTime: 0,
  });
  return { ...query, directory, results: search.data ?? [], isPending: query.isPending || (!!directory && search.isPending), isError: query.isError || search.isError, searchError: query.isError || search.isError, retrySearch: query.isError ? query.refetch : search.refetch };
}

/** Relationship/record pickers share the directory worker. */
export function usePersonPicker(text: string, limit = 10) {
  const { directory, ...query } = useDirectory();
  const search = useQuery({
    queryKey: ["local-person-search", directoryVersion(directory), text, limit],
    queryFn: ({ signal }) => searchPeople(directory!, text, { type: "person" }, "relevance", limit, signal),
    enabled: !!directory && !!text.trim(),
    gcTime: 0,
  });
  return { ...query, results: text.trim() ? search.data ?? [] : [], isSearching: !!text.trim() && (query.isFetching || (!!directory && search.isFetching)), searchError: query.isError || search.isError, retrySearch: query.isError ? query.refetch : search.refetch };
}

/**
 * Member counts per tag among profiles that already carry every `within` tag (optionally of one type),
 * i.e. how many people adding that tag to the selection would leave.
 */
export function useTagCounts(type?: "person" | "group", within: string[] = []) {
  const { directory, ...query } = useDirectory();
  const withinKey = within.join(",");
  const counts = useMemo(() => {
    const required = withinKey ? withinKey.split(",") : [];
    const result = new Map<string, number>();
    for (const profile of directory?.profiles ?? []) {
      if (type && profile.type !== type) continue;
      if (!required.every((tag) => profile.tagIds.includes(tag))) continue;
      for (const tag of profile.tagIds) result.set(tag, (result.get(tag) ?? 0) + 1);
    }
    return result;
  }, [directory, type, withinKey]);
  return { ...query, tags: directory?.tags, counts };
}

export interface IndexedPost { id: string; profileId: string | null; provider: string; occurredAt: string | null; text: string }

/** Post text search, loaded only by the search page. */
export function usePostSearch(text: string, enabled = true, limit = 50) {
  const query = useQuery({
    queryKey: ["local-post-search", text, limit],
    queryFn: ({ signal }) => searchPosts(text, limit, signal),
    enabled: enabled && !!text.trim(),
    staleTime: STALE_MS,
    gcTime: 0,
  });
  return { ...query, results: enabled && text.trim() ? query.data ?? [] : [] };
}

/**
 * Apply a local edit immediately. The server rebuilds its snapshot in the
 * background after a change, so the patched copy is kept until the regular
 * stale-time revalidation instead of being replaced by an immediate refetch.
 */
export function patchDirectoryProfile(client: QueryClient, id: string, patch: Partial<Record<string, unknown>>) {
  client.setQueryData<Snapshot<RawDirectory>>(directoryKey, (snapshot) => {
    if (!snapshot) return snapshot;
    const columns = snapshot.data.columns;
    const idColumn = columns.indexOf("id");
    const profiles = snapshot.data.profiles.map((row) => {
      if (row[idColumn] !== id) return row;
      const next = [...row];
      for (const [name, value] of Object.entries(patch)) {
        const index = columns.indexOf(name);
        if (index >= 0) next[index] = value;
      }
      return next;
    });
    return { ...snapshot, data: { ...snapshot.data, profiles } };
  });
}

/** Mirror a tag added to or removed from one profile in the local directory, adding a new tag to its dictionary. */
export function patchDirectoryTag(client: QueryClient, profileId: string, change: { add?: { id: string; name: string }; remove?: string }) {
  client.setQueryData<Snapshot<RawDirectory>>(directoryKey, (snapshot) => {
    if (!snapshot) return snapshot;
    const tags = [...snapshot.data.tags];
    const tagId = change.add?.id ?? change.remove!;
    let index = tags.findIndex(([id]) => id === tagId);
    if (index < 0 && change.add) index = tags.push([change.add.id, change.add.name]) - 1;
    if (index < 0) return snapshot;
    const { columns } = snapshot.data;
    const idColumn = columns.indexOf("id"), tagColumn = columns.indexOf("tag_ids");
    const profiles = snapshot.data.profiles.map((row) => {
      if (row[idColumn] !== profileId) return row;
      const current = (row[tagColumn] as number[]).filter((tag) => tag !== index);
      const next = [...row];
      next[tagColumn] = change.add ? [...current, index] : current;
      return next;
    });
    return { ...snapshot, data: { ...snapshot.data, tags, profiles } };
  });
}

/** Remove every persisted snapshot, for logout. */
export async function clearDirectory(client: QueryClient) {
  await client.cancelQueries({ queryKey: directoryKey });
  resetSearchWorkers();
  client.removeQueries({ queryKey: directoryKey });
  await Promise.all([del(STORE_KEY), del(POSTS_STORE_KEY)]);
}
