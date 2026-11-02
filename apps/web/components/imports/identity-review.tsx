"use client";

import Link from "next/link";
import {ArrowRight} from "lucide-react";
import {useEffect, useState} from "react";
import {apiFetch} from "../../lib/api";
import {Button} from "../ui/button";
import {providerLabel, Section} from "../ui/primitives";
import {RawData} from "./raw-data";
import styles from "./imports.module.css";

type Candidate = {
  id: string; provider: string; external_id: string; username?: string;
  source_profile_id: string; candidate_profile_id: string; candidate_display_name: string; evidence: unknown;
};

export function IdentityReview() {
  const [rows, setRows] = useState<Candidate[] | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    const response = await fetch("/api/v1/identity-candidates");
    if (response.ok) setRows(await response.json());
    else setError("无法读取匹配候选。");
  }
  useEffect(() => {void load();}, []);

  async function decide(row: Candidate, action: "accept" | "reject") {
    setBusy(true);
    setError("");
    try {
      const response = await apiFetch(`/api/v1/identity-candidates/${row.id}/${action}`, {method: "POST"});
      if (!response.ok) throw new Error("decide");
      setConfirming(null);
      await load();
    } catch {
      setError("审核未保存，请检查候选资料后重试。");
    } finally {
      setBusy(false);
    }
  }

  return <Section title="身份匹配待审核" className={styles.panel}>
    <p className={styles.note}>确认同一人后才移动账号、动态和关系。姓名相同不会触发自动合并。</p>
    {error && <p role="alert" className={styles.failed}>{error}</p>}
    {rows && !rows.length && <p className={styles.none}>当前没有待审核候选。独立来源账号继续保持独立。</p>}
    {rows?.map((row) => <article className={styles.candidate} key={row.id}>
      <p className={styles.pair}>
        <Link href={`/profiles/${row.source_profile_id}`}>{providerLabel(row.provider)} · {row.username || row.external_id}</Link>
        <ArrowRight size={14} aria-label="合并到" />
        <Link href={`/profiles/${row.candidate_profile_id}`}>{row.candidate_display_name}</Link>
      </p>
      <RawData value={row.evidence} label="来源证据" />
      {confirming === row.id ? <div className={styles.confirm}>
        <p>确认这两个资料属于同一个人，并将来源资料合并到「{row.candidate_display_name}」？</p>
        <div className={styles.actions}>
          <Button size="sm" variant="ghost" onClick={() => setConfirming(null)}>取消</Button>
          <Button size="sm" variant="primary" disabled={busy} onClick={() => void decide(row, "accept")}>确认合并</Button>
        </div>
      </div> : <div className={styles.actions}>
        <Button size="sm" disabled={busy} onClick={() => void decide(row, "reject")}>不是同一人</Button>
        <Button size="sm" variant="primary" disabled={busy} onClick={() => setConfirming(row.id)}>审核并确认</Button>
      </div>}
    </article>)}
  </Section>;
}
