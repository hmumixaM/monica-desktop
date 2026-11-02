"use client";

// Faces found by the home Mac. Monica holds boxes and decisions; embeddings stay on that machine.

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchJson } from "./queries";

/** Left, top, width, height as fractions of the image. */
export type Box = [number, number, number, number];

export interface FaceSample { id: string; media_id: string; box: Box; quality: number; score: number | null; post_id: string | null }

export interface FaceGroup {
  key: string;
  size: number;
  best: number | null;
  person: { profile_id: string; name: string; avatar_media_id: string | null } | null;
  faces: FaceSample[];
}

export interface FaceSummary { scanned: number; last_scan_at: string | null; faces: number; to_review: number; confirmed: number; people: number }

export interface FaceState {
  id: string; status: "pending" | "confirmed" | "ignored"; profile_id: string | null;
  suggested_profile_id: string | null; rejected_profile_id: string | null; decided_at: string | null;
}

export type FaceDecision = { face_ids: string[]; action: "confirm"; profile_id: string }
  | { face_ids: string[]; action: "not" | "ignore"; profile_id?: string };

export interface TaggedFace { id: string; media_id: string; box: Box; status: "pending" | "confirmed"; score: number | null; profile_id: string; name: string }

export interface Appearance {
  id: string; media_id: string; box: Box; post_id: string; occurred_at: string; provider: string; author: string | null;
  others: { profile_id: string; name: string }[] | null;
}

const keys = {
  all: ["faces"] as const,
  summary: ["faces", "summary"] as const,
  groups: ["faces", "groups"] as const,
  media: (ids: string[]) => ["faces", "media", ids.join(",")] as const,
  person: (id: string) => ["faces", "person", id] as const,
};

export function useFaceSummary() {
  return useQuery({ queryKey: keys.summary, queryFn: () => fetchJson<FaceSummary>("/api/v1/faces/summary") });
}

export function useFaceGroups() {
  return useInfiniteQuery({
    queryKey: keys.groups,
    initialPageParam: 0,
    queryFn: ({ pageParam }) => fetchJson<{ items: FaceGroup[]; next_offset: number | null }>(`/api/v1/faces/groups?offset=${pageParam}`),
    getNextPageParam: (page) => page.next_offset ?? undefined,
  });
}

const post = (path: string, body: unknown) =>
  fetchJson<{ previous: FaceState[] }>(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

/** Decides faces and returns their earlier states, which `useRestoreFaces` puts back. */
export function useDecideFaces() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (decision: FaceDecision) => post("/api/v1/faces/decide", decision),
    onMutate: ({ face_ids }) => {
      const decided = new Set(face_ids);
      client.setQueryData<{ pages: { items: FaceGroup[]; next_offset: number | null }[]; pageParams: unknown[] }>(keys.groups, (data) => data && {
        ...data,
        pages: data.pages.map((page) => ({
          ...page,
          items: page.items.map((group) => {
            const faces = group.faces.filter((face) => !decided.has(face.id));
            return { ...group, faces, size: group.size - (group.faces.length - faces.length) };
          }).filter((group) => group.faces.length),
        })),
      });
    },
    onSettled: () => client.invalidateQueries({ queryKey: keys.all }),
  });
}

export function useRestoreFaces() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (states: FaceState[]) => post("/api/v1/faces/restore", states),
    onSettled: () => client.invalidateQueries({ queryKey: keys.all }),
  });
}

export function useFacesInMedia(mediaIds: string[]) {
  return useQuery({
    queryKey: keys.media(mediaIds),
    queryFn: () => fetchJson<{ faces: TaggedFace[] }>(`/api/v1/faces/in-media?ids=${mediaIds.join(",")}`),
    enabled: mediaIds.length > 0,
  });
}

export function useAppearances(profileId: string) {
  return useInfiniteQuery({
    queryKey: keys.person(profileId),
    initialPageParam: 0,
    queryFn: ({ pageParam }) => fetchJson<{ total: number; items: Appearance[]; next_offset: number | null }>(
      `/api/v1/faces/people/${profileId}?offset=${pageParam}`),
    getNextPageParam: (page) => page.next_offset ?? undefined,
  });
}
