"use client";

import { Plus, Tags, X } from "lucide-react";
import Link from "next/link";
import { type FormEvent, type KeyboardEvent, useState } from "react";

import { useTagCounts } from "../../lib/directory";
import { type TagRef, useAttachTag, useCreateTag, useDetachTag } from "../../lib/queries";
import { Button } from "../ui/button";
import { Input } from "../ui/field";
import { Section } from "../ui/primitives";
import styles from "./profile-tags.module.css";

const SUGGESTIONS = 8;
const SOURCE_NAMES: Record<string, string> = { manual: "手动添加", wechat: "来自微信标签", monica: "来自 Monica", instagram: "来自 Instagram", linkedin: "来自 LinkedIn" };

export function ProfileTags({ profileId, tags }: { profileId: string; tags: (TagRef & { source_type: string })[] }) {
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const { tags: known, counts } = useTagCounts();
  const attach = useAttachTag(profileId);
  const detach = useDetachTag(profileId);
  const create = useCreateTag();
  const busy = attach.isPending || detach.isPending || create.isPending;
  const name = text.trim();
  const needle = name.toLocaleLowerCase();
  const onProfile = new Set(tags.map((tag) => tag.id));
  const suggestions = [...(known ?? [])].filter(([id, tag]) => !onProfile.has(id) && tag.toLocaleLowerCase().includes(needle))
    .sort(([a], [b]) => (counts.get(b) ?? 0) - (counts.get(a) ?? 0)).slice(0, SUGGESTIONS);
  const exact = [...(known ?? [])].find(([, tag]) => tag === name);

  function add(tag: TagRef) {
    setError("");
    attach.mutate(tag, { onSuccess: () => setText(""), onError: () => setError("没能添加标签，请再试一次。") });
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!name || busy) return;
    if (exact) return add({ id: exact[0], name: exact[1] });
    create.mutate(name.slice(0, 255), { onSuccess: add, onError: () => setError("没能创建标签，请再试一次。") });
  }
  function finish() {
    setAdding(false);
    setText("");
    setError("");
  }
  function escape(event: KeyboardEvent) {
    if (event.key === "Escape") finish();
  }

  return <Section title="标签" icon={<Tags size={15} aria-hidden="true" />}
    action={!adding && <Button variant="ghost" size="sm" onClick={() => setAdding(true)}><Plus size={13} />添加</Button>}>
    {tags.length > 0 && <div className={styles.chips}>
      {tags.map((tag) => <span key={tag.id} className={styles.chip} title={SOURCE_NAMES[tag.source_type] ?? tag.source_type}>
        <Link href={`/profiles?tag=${tag.id}`}>{tag.name}</Link>
        <button type="button" disabled={busy} aria-label={`移除标签${tag.name}`}
          title={tag.source_type === "manual" ? "移除标签" : "移除标签，之后的导入不会再加回"}
          onClick={() => detach.mutate(tag, { onError: () => setError("没能移除标签，请再试一次。") })}><X size={11} /></button>
      </span>)}
    </div>}
    {!tags.length && !adding && <button className={styles.empty} type="button" onClick={() => setAdding(true)}><Plus size={14} />添加标签</button>}
    {adding && <form className={styles.form} onSubmit={submit} onKeyDown={escape}>
      <Input autoFocus value={text} maxLength={255} onChange={(event) => { setText(event.target.value); setError(""); }}
        placeholder="输入标签，回车添加" aria-label="标签名称" />
      <div className={styles.suggestions}>
        {suggestions.map(([id, tag]) => <button key={id} type="button" disabled={busy} onClick={() => add({ id, name: tag })}>
          {tag}<b>{counts.get(id) ?? 0}</b></button>)}
        {name && !exact && <button type="submit" className={styles.create} disabled={busy}><Plus size={12} />新建“{name}”</button>}
      </div>
      <Button variant="ghost" size="sm" className={styles.done} onClick={finish}>完成</Button>
    </form>}
    {error && <p className={styles.error} role="alert">{error}</p>}
  </Section>;
}
