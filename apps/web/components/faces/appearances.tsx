"use client";

import { ScanFace } from "lucide-react";
import Link from "next/link";

import { useAppearances } from "../../lib/faces";
import { LoadMore } from "../ui/load-more";
import { formatDateTime, Section } from "../ui/primitives";
import { FaceCrop } from "./face-crop";
import styles from "./faces.module.css";

/** Photos this person was confirmed in, including other people's posts. Renders nothing until there are some. */
export function Appearances({ profileId, name }: { profileId: string; name: string }) {
  const photos = useAppearances(profileId);
  const items = photos.data?.pages.flatMap((page) => page.items) ?? [];
  const total = photos.data?.pages[0]?.total ?? 0;
  if (!items.length) return null;
  return <Section title={`出现在照片里 · ${total}`} icon={<ScanFace size={15} aria-hidden="true" />}
    action={<Link href="/faces" className={styles.post}>确认更多</Link>}>
    <ul className={styles.strip}>{items.map((photo) => {
      const others = photo.others?.map((other) => other.name) ?? [];
      return <li key={`${photo.media_id}-${photo.post_id}`}>
        <Link href={`/timeline?event=${photo.post_id}`} title={others.length ? `和 ${others.join("、")}` : undefined}>
          <FaceCrop mediaId={photo.media_id} box={photo.box} size={96} label={`${name}，${formatDateTime(photo.occurred_at, false)}`} />
          <span>{formatDateTime(photo.occurred_at, false)}{photo.author && photo.author !== name ? ` · ${photo.author}` : ""}</span>
        </Link>
      </li>;
    })}</ul>
    {photos.hasNextPage && <LoadMore cursor={String(items.length)} busy={photos.isFetchingNextPage} error={photos.isFetchNextPageError}
      onLoad={() => void photos.fetchNextPage()} />}
  </Section>;
}
