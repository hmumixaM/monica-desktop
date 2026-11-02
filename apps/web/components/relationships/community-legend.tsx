"use client";

import { Check, Pencil, RotateCcw, Sparkles, X } from "lucide-react";
import { type FormEvent, useState } from "react";

import { useRenameCommunity } from "../../lib/ai";
import type { OrbitCommunity } from "../../lib/orbit";
import { IconButton } from "../ui/button";
import { Input } from "../ui/field";
import { useToast } from "../ui/toast";
import styles from "./orbit.module.css";

function Rename({ community, onDone, onRenamed }: { community: OrbitCommunity; onDone: () => void; onRenamed?: () => void }) {
  const rename = useRenameCommunity();
  const toast = useToast();
  const [value, setValue] = useState(community.label);
  const save = (label: string | null) => rename.mutate({ key: community.id, label }, {
    onSuccess: () => { toast.show({ message: label ? `已改名为「${label}」` : "已恢复自动名称" }); onDone(); onRenamed?.(); },
    onError: () => toast.show({ message: "没能保存名称，请重试" }),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const label = value.trim();
    if (!label || label === community.label) onDone();
    else save(label);
  };
  return <form className={styles.rename} onSubmit={submit} onKeyDown={(event) => event.key === "Escape" && onDone()}>
    <Input aria-label={`${community.label} 的新名称`} value={value} maxLength={40} autoFocus disabled={rename.isPending}
      onChange={(event) => setValue(event.target.value)} onFocus={(event) => event.target.select()} />
    <IconButton size="sm" type="submit" label="保存名称" disabled={rename.isPending}><Check size={14} /></IconButton>
    {community.label_source === "manual" && <IconButton size="sm" label="恢复自动名称" disabled={rename.isPending} onClick={() => save(null)}>
      <RotateCcw size={14} />
    </IconButton>}
    <IconButton size="sm" label="取消" onClick={onDone}><X size={14} /></IconButton>
  </form>;
}

/**
 * Communities, optionally as filters; each name can be changed in place. Hand-typed names win over the model's.
 * Without `onFocus` the rows are plain labels.
 */
export function CommunityLegend({ communities, colors, focus = null, onFocus, onRenamed }: {
  communities: OrbitCommunity[]; colors: Map<string, string>; focus?: string | null; onFocus?: (id: string | null) => void;
  onRenamed?: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  return <ul className={styles.legend}>{communities.map((community) => {
    const content = <>
      <i style={{ background: colors.get(community.id) }} />
      <span>{community.label}{community.label_source === "model" && <Sparkles size={11} className={styles.modelMark} aria-label="模型命名" />}</span>
      <b>{community.size}</b>
    </>;
    return <li key={community.id}>
    {editing === community.id ? <Rename community={community} onDone={() => setEditing(null)} onRenamed={onRenamed} /> : <>
      {onFocus ? <button type="button" className={styles.filter} aria-pressed={focus === community.id}
        onClick={() => onFocus(focus === community.id ? null : community.id)}>{content}</button>
        : <div className={styles.filter}>{content}</div>}
      {community.id !== "other" && <IconButton size="sm" className={styles.renameButton} label={`重命名 ${community.label}`}
        onClick={() => setEditing(community.id)}><Pencil size={13} /></IconButton>}
    </>}
    </li>;
  })}</ul>;
}
