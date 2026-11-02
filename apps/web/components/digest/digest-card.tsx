"use client";

import { ArrowRight, Newspaper } from "lucide-react";
import Link from "next/link";

import { useDigests } from "../../lib/ai";
import { weekRange } from "./week";
import styles from "./digest.module.css";

/** The latest weekly digest on the home page; nothing when the model is off or no digest exists yet. */
export function DigestCard() {
  const digests = useDigests();
  const latest = digests.data?.enabled ? digests.data.digests[0] : undefined;
  if (!latest) return null;
  return <Link className={styles.card} href={`/digest?week=${latest.week_start}`}>
    <span className={styles.cardIcon}><Newspaper size={18} aria-hidden="true" /></span>
    <span className={styles.cardText}>
      <small>每周回顾 · {weekRange(latest.week_start)}</small>
      <span>{latest.summary ?? "本周的统计已经整理好，文字回顾还在生成。"}</span>
    </span>
    <ArrowRight size={16} className={styles.cardArrow} aria-hidden="true" />
  </Link>;
}
