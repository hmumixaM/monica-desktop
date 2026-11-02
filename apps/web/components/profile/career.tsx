"use client";

import { BriefcaseBusiness, ExternalLink, GraduationCap, MapPin } from "lucide-react";

import { localMediaUrl } from "../../lib/media";
import { Panel, SourceMark, Stat } from "../ui/primitives";
import { factParts, sortFacts, textValue } from "./fields";
import styles from "./profile.module.css";
import type { Profile, StructuredFact } from "./types";

function OpenLink({ href }: { href: string }) {
  return <a className={styles.headerLink} href={href} target="_blank" rel="noreferrer">打开主页<ExternalLink size={12} /></a>;
}

function Fact({ fact }: { fact: StructuredFact }) {
  const { institution, title, field, dates, location, description, current } = factParts(fact);
  return <li className={styles.fact} data-current={current ? "" : undefined}>
    <strong>{institution || title || "已收录经历"}</strong>
    {institution && title && <span>{title}</span>}
    {field && <span>{field}</span>}
    {(dates || location) && <small>{[dates, location].filter(Boolean).join(" · ")}</small>}
    {description && <details><summary>经历详情</summary><p>{description}</p></details>}
  </li>;
}

/** Career and education as two short timelines, current roles first. */
export function LinkedInDossier({ profile }: { profile: Profile }) {
  const facts = profile.presentation?.facts || profile.structured_facts || [];
  const linkedin = profile.presentation?.sources.linkedin || {};
  if (!Object.keys(linkedin).length && !facts.length) return null;
  const url = textValue(linkedin.profile_url);
  const location = textValue(linkedin.location);
  const connected = textValue(linkedin.connected_on);
  const columns = (["employment", "education"] as const).filter((type) => facts.some((fact) => fact.fact_type === type));
  return <Panel className={styles.block} label="职业与教育">
    <header className={styles.blockHeader}>
      <h2><SourceMark provider="linkedin" size={11} decorative />职业与教育</h2>
      {url && <OpenLink href={url} />}
    </header>
    {profile.linkedin_cover_media_id && <img className={styles.linkedinCover} src={localMediaUrl(profile.linkedin_cover_media_id)} alt="LinkedIn 背景图" loading="lazy" />}
    {(location || connected || linkedin.open_to_work === true) && <p className={styles.metaLine}>
      {location && <span><MapPin size={13} aria-hidden="true" />{location}</span>}
      {connected && <span>建立联系 {connected}</span>}
      {linkedin.open_to_work === true && <span className={styles.flag}>寻求工作机会</span>}
    </p>}
    <div className={styles.career}>
      {columns.map((type) => <section key={type}>
        <h3>{type === "employment" ? <BriefcaseBusiness size={14} aria-hidden="true" /> : <GraduationCap size={14} aria-hidden="true" />}{type === "employment" ? "职业经历" : "教育经历"}</h3>
        <ol>{sortFacts(facts.filter((fact) => fact.fact_type === type)).map((fact, index) => <Fact fact={fact} key={index} />)}</ol>
      </section>)}
    </div>
  </Panel>;
}

export function InstagramDossier({ profile }: { profile: Profile }) {
  const instagram = profile.presentation?.sources.instagram || {};
  if (!Object.keys(instagram).length) return null;
  const numbers = ([["粉丝", instagram.followers_count], ["关注", instagram.followees_count], ["主页帖子", instagram.media_count]] as const).filter(([, value]) => value != null);
  const url = textValue(instagram.profile_url);
  return <Panel className={styles.block} label="Instagram">
    <header className={styles.blockHeader}><h2><SourceMark provider="instagram" size={11} decorative />Instagram</h2>{url && <OpenLink href={url} />}</header>
    {numbers.length > 0 && <div className={styles.statRow}>{numbers.map(([label, value]) => <Stat key={label} size="sm" value={Number(value).toLocaleString("zh-CN")} label={label} />)}</div>}
  </Panel>;
}
