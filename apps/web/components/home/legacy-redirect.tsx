"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

const CARRIED = ["provider", "month", "event"];

/** The feed used to live at /dashboard#timeline; bookmarks and shared links still point there. */
export function LegacyTimelineRedirect() {
  const router = useRouter();
  useEffect(() => {
    const url = new URL(window.location.href);
    const legacy = url.hash === "#timeline" || url.searchParams.has("event") || url.searchParams.get("view") === "photos";
    if (!legacy) return;
    const next = new URLSearchParams();
    for (const key of CARRIED) {
      const value = url.searchParams.get(key);
      if (value) next.set(key, value);
    }
    router.replace(next.size ? `/timeline?${next}` : "/timeline");
  }, [router]);
  return null;
}
