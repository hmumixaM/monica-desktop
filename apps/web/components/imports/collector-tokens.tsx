"use client";

import {useMutation, useQuery, useQueryClient} from "@tanstack/react-query";
import {Copy, KeyRound, Plus} from "lucide-react";
import {type FormEvent, useState} from "react";
import {fetchJson} from "../../lib/queries";
import {Button} from "../ui/button";
import {Dialog} from "../ui/dialog";
import {Field, Input, Select} from "../ui/field";
import {formatDateTime, Section} from "../ui/primitives";
import {useToast} from "../ui/toast";
import styles from "./imports.module.css";

type Token = {id: string; source: string; label: string; created_at: string; last_used_at: string | null; revoked_at: string | null};

const SOURCES: Record<string, string> = {
  faces: "人脸识别（家里的 Mac）", wechat: "微信", instagram: "Instagram", linkedin: "LinkedIn", monica: "Monica",
};
const key = ["import-tokens"] as const;

/** A token per collector, so one machine can be cut off without touching the others. */
export function CollectorTokens() {
  const client = useQueryClient();
  const toast = useToast();
  const tokens = useQuery({queryKey: key, queryFn: () => fetchJson<Token[]>("/api/v1/imports/tokens")});
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<Token | null>(null);
  const revoke = useMutation({
    mutationFn: (token: Token) => fetchJson(`/api/v1/imports/tokens/${token.id}/revoke`, {method: "POST"}),
    onSuccess: (_, token) => {
      setRevoking(null);
      toast.show({message: `已停用「${token.label}」，这台采集器的请求会立即被拒绝`});
      void client.invalidateQueries({queryKey: key});
    },
  });
  const active = (tokens.data ?? []).filter((token) => !token.revoked_at);

  return <Section title="采集器令牌" className={styles.panel}
    action={<Button size="sm" variant="ghost" onClick={() => setCreating(true)}><Plus size={14} />新令牌</Button>}>
    {active.length ? <ul className={styles.tokens}>
      {active.map((token) => <li key={token.id}>
        <KeyRound size={15} aria-hidden="true" />
        <div>
          <strong>{token.label}</strong>
          <small>{SOURCES[token.source] ?? token.source} · 创建于 {formatDateTime(token.created_at, false)}
            {" · "}{token.last_used_at ? `最近使用 ${formatDateTime(token.last_used_at)}` : "还没用过"}</small>
        </div>
        <Button size="sm" variant="ghost" onClick={() => setRevoking(token)}>停用</Button>
      </li>)}
    </ul> : <p className={styles.none}>{tokens.isPending ? "读取中…" : "还没有令牌。有令牌的来源，每个请求都必须带上它。"}</p>}
    {creating && <CreateToken onClose={() => setCreating(false)} />}
    {revoking && <Dialog title="停用令牌" description={`「${revoking.label}」停用后不能恢复，需要给采集器换一个新令牌。`}
      onClose={() => setRevoking(null)} locked={revoke.isPending}>
      <div className={styles.dialogActions}>
        <Button variant="ghost" onClick={() => setRevoking(null)}>取消</Button>
        <Button variant="danger" disabled={revoke.isPending} onClick={() => revoke.mutate(revoking)}>停用</Button>
      </div>
    </Dialog>}
  </Section>;
}

function CreateToken({onClose}: {onClose: () => void}) {
  const client = useQueryClient();
  const toast = useToast();
  const [source, setSource] = useState("faces");
  const [label, setLabel] = useState("家里的 MacBook");
  const create = useMutation({
    mutationFn: () => fetchJson<Token & {token: string}>("/api/v1/imports/tokens", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({source, label: label.trim()}),
    }),
    onSuccess: () => void client.invalidateQueries({queryKey: key}),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  async function copy(token: string) {
    await navigator.clipboard.writeText(token);
    toast.show({message: "已复制令牌"});
  }

  if (create.data) return <Dialog title="令牌已创建" description="只显示这一次。放进采集器的配置文件，不要提交到仓库。" onClose={onClose}>
    <div className={styles.secret}>
      <code>{create.data.token}</code>
      <Button size="sm" onClick={() => void copy(create.data.token)}><Copy size={14} />复制</Button>
    </div>
    <div className={styles.dialogActions}><Button onClick={onClose}>完成</Button></div>
  </Dialog>;

  return <Dialog title="新令牌" onClose={onClose} locked={create.isPending}>
    <form className={styles.tokenForm} onSubmit={submit}>
      <Field label="来源">
        <Select value={source} onChange={(event) => setSource(event.target.value)}>
          {Object.entries(SOURCES).map(([value, name]) => <option key={value} value={value}>{name}</option>)}
        </Select>
      </Field>
      <Field label="名称" hint="用来认出是哪台机器">
        <Input value={label} maxLength={120} required onChange={(event) => setLabel(event.target.value)} />
      </Field>
      {create.isError && <p className={styles.failed}>没能创建，请重试。</p>}
      <div className={styles.dialogActions}>
        <Button type="button" variant="ghost" onClick={onClose}>取消</Button>
        <Button type="submit" disabled={create.isPending || !label.trim()}>创建</Button>
      </div>
    </form>
  </Dialog>;
}
