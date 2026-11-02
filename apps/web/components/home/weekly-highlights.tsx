"use client";

import Link from "next/link";

import { Sparkles } from "lucide-react";

import { useWeeklyHighlights } from "../../lib/ai";
import { weekRange } from "../digest/week";
import { PersonLink } from "../ui/person-link";
import { Avatar } from "../ui/primitives";
import styles from "./home.module.css";

/** Saved weekly notes are the main home section; each card opens the full profile. */
export function WeeklyHighlights() {
  const weekly = useWeeklyHighlights();
  const data = weekly.data;
  if (!data?.enabled || !data.people.length) return null;
  return <section className={styles.weeklyHighlights} aria-labelledby="weekly-highlights-title">
    <div className={styles.weeklyHeading}><h2 id="weekly-highlights-title"><Sparkles size={16} aria-hidden="true" />朋友近况
      <small>{data.week_start && weekRange(data.week_start)}</small></h2>
    <Link href={`/digest?view=people&week=${data.week_start}`}>查看全部近况</Link></div>
    <div className={styles.weeklyGrid}>{data.people.map((person) =>
      <PersonLink className={styles.weeklyCard} href={`/profiles/${person.profile_id}`} key={person.profile_id}>
        <Avatar name={person.name} mediaId={person.avatar_media_id} size={44} round />
        <span><strong>{person.name}</strong><small>{person.summary}</small></span>
      </PersonLink>)}</div>
  </section>;
}
