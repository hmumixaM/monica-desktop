"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import type { Box } from "./faces";
import { fetchJson } from "./queries";

export type PhotoState = "all" | "pending" | "confirmed";
export type PhotoSort = "time" | "score";
export interface PhotoFace {
  id: string; media_id: string; box: Box; status: "pending" | "confirmed"; score: number | null;
  profile_id: string | null; name: string | null;
}
export interface PhotoSource {
  entity_type: string; entity_id: string; provider: string | null; occurred_at: string | null;
  author: string | null; conversation_type: string | null; conversation_name: string | null; href: string | null;
}
export interface ProfilePhoto {
  media_id: string; occurred_at: string; score: number | null; pending: boolean; confirmed: boolean;
  width: number | null; height: number | null; faces: PhotoFace[]; sources: PhotoSource[];
}
export interface PhotoStatus { counts: Record<PhotoState, number>; revision: string }
export interface PhotoPage extends PhotoStatus { items: ProfilePhoto[]; next_cursor: string | null }
export const photoKey = (id: string, state: PhotoState, sort: PhotoSort) => ["profile-photo-gallery", id, state, sort] as const;
export const photoStatusKey = (id: string) => ["profile-photo-status", id] as const;

export function useProfilePhotos(id: string, state: PhotoState, sort: PhotoSort) {
  return useInfiniteQuery({
    queryKey: photoKey(id, state, sort), initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => {
      const search = new URLSearchParams({ state, sort, limit: "24" });
      if (pageParam) search.set("cursor", pageParam);
      return fetchJson<PhotoPage>(`/api/v1/faces/people/${id}/photos?${search}`, { signal });
    },
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    staleTime: Infinity, gcTime: Infinity, retry: false,
    refetchOnMount: false, refetchOnWindowFocus: false, refetchOnReconnect: false,
  });
}

export function usePhotoStatus(id: string, enabled: boolean) {
  return useQuery({ queryKey: photoStatusKey(id),
    queryFn: ({ signal }) => fetchJson<PhotoStatus>(`/api/v1/faces/people/${id}/photos/status`, { signal }),
    enabled, refetchInterval: 60_000, staleTime: 30_000,
  });
}

export function usePhotoReading(id: string) {
  return useQuery({ queryKey: ["photo-reading", id],
    queryFn: ({ signal }) => fetchJson<{ readings: { kind: string; entity_id: string; summary: string;
      items: { media_id?: string; description?: string; text?: string }[] }[] }>(`/api/v1/faces/media/${id}/reading`, { signal }),
  });
}
