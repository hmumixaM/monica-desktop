"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { apiFetch } from "../../lib/api";
import { clearDirectory } from "../../lib/directory";
import { Button } from "../ui/button";
import { Section } from "../ui/primitives";
import styles from "./settings.module.css";

type Archived = { id: string; display_name: string; target_id: string };
export function ArchivedProfiles() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["archived-profiles"], queryFn: async () => {
    const response = await apiFetch("/api/v1/archived-profiles");
    if (!response.ok) throw new Error("读取失败");
    return await response.json() as Archived[];
  }});
  const restore = useMutation({ mutationFn: async (id: string) => {
    const response = await apiFetch(`/api/v1/profiles/${id}/restore`, { method: "POST" });
    if (!response.ok) throw new Error("恢复失败");
    await clearDirectory(client);
    await client.invalidateQueries({ queryKey: ["profile", id] });
    await query.refetch();
  }});
  return <Section title="已归档的空资料" className={styles.panel}>
    {query.isError && <Button onClick={() => void query.refetch()}>重新读取</Button>}
    {restore.isError && <p className={styles.error} role="alert">恢复失败，请重试。</p>}
    {query.data?.length === 0 && <p>没有已归档的资料。</p>}
    {!!query.data?.length && <ul className={styles.list}>{query.data.map((profile) => <li key={profile.id} className={styles.row}>
      <div className={styles.text}><strong>{profile.display_name}</strong><Link href={`/profiles/${profile.target_id}`}>查看合并后的资料</Link></div>
      <Button size="sm" disabled={restore.isPending} onClick={() => restore.mutate(profile.id)}>恢复</Button>
    </li>)}</ul>}
  </Section>;
}
