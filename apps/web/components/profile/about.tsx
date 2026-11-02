"use client";

import { ExternalLink } from "lucide-react";

import { Section, SourceMark, providerLabel } from "../ui/primitives";
import { summarizeValue, textValue } from "./fields";
import styles from "./profile.module.css";
import type { Profile } from "./types";

export function ReadableValue({ value }: { value: unknown }) {
  if (typeof value === "string") return /^https?:\/\//.test(value)
    ? <a className={styles.external} href={value} target="_blank" rel="noreferrer">{value}<ExternalLink size={11} /></a>
    : <span className={styles.value}>{value}</span>;
  if (Array.isArray(value)) return <span className={styles.valueList}>{value.map((item, index) => <ReadableValue value={item} key={index} />)}</span>;
  return <span className={styles.value}>{summarizeValue(value)}</span>;
}

/**
 * Self-descriptions from each platform; several Instagram accounts are listed one after another.
 * The same text on several platforms is shown once under all of them, and text already used as the summary is skipped.
 */
export function PlatformBios({ profile }: { profile: Profile }) {
  const sources = profile.presentation?.sources || {};
  const accounts = profile.presentation?.instagram_accounts ?? [];
  const summary = profile.summary?.trim();
  const bios = ["wechat", "instagram", "linkedin"].flatMap((source) => {
    const data = sources[source] || {};
    const bio = source === "instagram" && accounts.length > 1
      ? accounts.filter((account) => account.biography).map((account) => `@${account.username}\n${textValue(account.biography)}`).join("\n\n")
      : textValue(data.biography || data.about || data.signature);
    const headline = textValue(data.headline);
    return bio || headline ? [{ sources: [source], bio: bio.trim() === summary ? "" : bio, headline }] : [];
  }).filter(({ bio, headline }) => bio || headline).reduce<{ sources: string[]; bio: string; headline: string }[]>((merged, item) => {
    const same = merged.find((other) => other.bio === item.bio && other.headline === item.headline);
    if (same) same.sources.push(...item.sources);
    else merged.push(item);
    return merged;
  }, []);
  if (!bios.length) return null;
  return <div className={styles.bios}>
    {bios.map(({ sources: from, bio, headline }) => <figure key={from.join()} className={styles.bio} data-source={from[0]}>
      <figcaption>{from.map((source) => <span key={source}><SourceMark provider={source} size={11} decorative />{providerLabel(source)}</span>)}</figcaption>
      {headline && <strong>{headline}</strong>}
      {bio && <details><summary><span>{bio}</span></summary><p>{bio}</p></details>}
    </figure>)}
  </div>;
}

export function WeChatDetails({ profile }: { profile: Profile }) {
  const data = profile.presentation?.sources.wechat || {};
  const region = textValue(data.region) || [data.country, data.province, data.city].filter(Boolean).join(" · ");
  const rows: [string, unknown][] = profile.profile_type === "group"
    ? [["群名称", data.nickname], ["备注", data.remark]]
    : [["微信号", data.alias], ["昵称", data.nickname], ["备注", data.remark], ["地区", region], ["添加方式", data.add_source],
      ["首次群内出现", data.first_group_seen], ["最近群内出现", data.last_group_seen]];
  const signature = textValue(data.signature);
  const showSignature = !!signature && signature.trim() !== profile.summary?.trim();
  if (!rows.some(([, value]) => value) && !data.group_nicknames && !showSignature) return null;
  return <Section title="微信资料" icon={<SourceMark provider="wechat" size={11} decorative />}>
    <dl className={styles.facts}>{rows.filter(([, value]) => value).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{textValue(value)}</dd></div>)}</dl>
    {!!data.group_nicknames && <details className={styles.more}><summary>微信群内显示名</summary><p>{textValue(data.group_nicknames)}</p></details>}
    {showSignature && <blockquote className={styles.signature}>{signature}</blockquote>}
  </Section>;
}
