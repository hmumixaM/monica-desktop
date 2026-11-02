"use client";

import { useQuery } from "@tanstack/react-query";
import { useDeferredValue, useState } from "react";

import { fetchJson } from "../../lib/queries";
import { Input } from "./field";
import styles from "./person-picker.module.css";
import { Avatar } from "./primitives";

export type PickedPerson = { id: string; display_name: string; avatar_media_id: string | null };

/** Search people by name and choose one. */
export function PersonPicker({ onPick, placeholder = "搜索联系人", autoFocus }: {
  onPick: (person: PickedPerson) => void; placeholder?: string; autoFocus?: boolean;
}) {
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query.trim());
  const matches = useQuery({
    queryKey: ["person-picker", deferred],
    queryFn: () => fetchJson<PickedPerson[]>(`/api/v1/profiles?q=${encodeURIComponent(deferred)}&profile_type=person&limit=8`),
    enabled: deferred.length > 0,
    staleTime: 60_000,
  });
  return <div className={styles.picker}>
    <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={placeholder} autoFocus={autoFocus}
      aria-label={placeholder} />
    {deferred && matches.data && (matches.data.length
      ? <ul>{matches.data.map((person) => <li key={person.id}>
        <button type="button" onClick={() => onPick(person)}>
          <Avatar name={person.display_name} mediaId={person.avatar_media_id} size={24} />{person.display_name}
        </button>
      </li>)}</ul>
      : <p>没有找到「{deferred}」</p>)}
  </div>;
}
