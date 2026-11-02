"use client";

import Link from "next/link";

import { useFacesInMedia } from "../../lib/faces";
import { PersonLink } from "../ui/person-link";
import { FaceCrop } from "./face-crop";
import styles from "./faces.module.css";

/** Who is in a post's photos, one chip per person: confirmed people link to their profile, suggestions to the review page. */
export function PhotoPeople({ mediaIds }: { mediaIds: string[] }) {
  const faces = useFacesInMedia(mediaIds).data?.faces ?? [];
  if (!faces.length) return null;
  const people = [...Map.groupBy(faces, (face) => face.profile_id).values()].map((group) =>
    ({ face: group.find((face) => face.status === "confirmed") ?? group[0], count: group.length }));
  return <section className={styles.tags} aria-label="照片里的人">
    <h3>照片里</h3>
    <ul>{people.map(({ face, count }) => {
      const crop = <FaceCrop mediaId={face.media_id} box={face.box} size={28} label="" />;
      const times = count > 1 && <small>× {count}</small>;
      return <li key={face.profile_id} data-status={face.status}>
        {face.status === "confirmed"
          ? <PersonLink href={`/profiles/${face.profile_id}?tab=photos&media=${face.media_id}`}>{crop}{face.name}{times}</PersonLink>
          : <Link href={`/profiles/${face.profile_id}?tab=photos&media=${face.media_id}`} title="查看候选照片">{crop}可能是 {face.name}{times}</Link>}
      </li>;
    })}</ul>
  </section>;
}
