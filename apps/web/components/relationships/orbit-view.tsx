"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type CSSProperties, useMemo, useState } from "react";

import { useMediaQuery } from "../../lib/media-query";
import { localMediaUrl } from "../../lib/media";
import { type Orbit, type OrbitPerson, type OrbitWindow, ORBIT_SIZE, orbitLabel, placeOrbit, recencyOpacity, RING_EDGES } from "../../lib/orbit";
import { fetchJson } from "../../lib/queries";
import { cssVar, useResolvedTheme } from "../../lib/theme";
import { Avatar, EmptyState, exactCount, Failure, formatCount, relativeDays, Skeleton } from "../ui/primitives";
import { Segmented } from "../ui/segmented";
import { CommunityLegend } from "./community-legend";
import styles from "./orbit.module.css";

const RING_NAMES = ["最亲近", "常联系", "朋友", "熟人"];
const WINDOWS: { value: OrbitWindow; label: string }[] = [{ value: "3", label: "近 3 个月" }, { value: "12", label: "近 12 个月" }];

function useOrbit() {
  return useQuery({ queryKey: ["orbit"], queryFn: () => fetchJson<Orbit>("/api/v1/insights/orbit"), staleTime: 10 * 60_000 });
}

/** Community colours from `--graph-communities`, re-read when the palette changes. */
function useCommunityColors(ids: string[]) {
  const theme = useResolvedTheme();
  const key = ids.join("|");
  return useMemo(() => {
    if (!key) return new Map<string, string>();
    const palette = cssVar("--graph-communities").split(",").map((part) => part.trim()).filter(Boolean);
    const faint = cssVar("--ink-3");
    let next = 0;
    return new Map(key.split("|").map((id) => [id, id === "other" || !palette.length ? faint : palette[next++ % palette.length]]));
  }, [key, theme]);
}

/**
 * 亲疏圈: Dunbar rings of the people you talk to, closest in the middle, grouped around the circle by the
 * communities they share. Switching windows moves everyone to their ring for that period.
 */
export function OrbitView() {
  const orbit = useOrbit();
  const router = useRouter();
  const hover = useMediaQuery("(hover: hover)");
  const [span, setSpan] = useState<OrbitWindow>("12");
  const [focus, setFocus] = useState<string | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const data = orbit.data;
  const placements = useMemo(() => data ? placeOrbit(data, span) : new Map(), [data, span]);
  const colors = useCommunityColors(data?.communities.map((community) => community.id) ?? []);
  const people = useMemo(() => new Map(data?.people.map((person) => [person.profile_id, person])), [data]);

  if (orbit.isError) return <Failure onRetry={() => void orbit.refetch()}>暂时无法读取亲疏圈。</Failure>;
  if (!data) return <Skeleton height={560} />;
  if (!data.people.length) return <EmptyState title="还没有足够的聊天记录">导入私聊或通话后，这里会按联系频率排出亲疏圈。</EmptyState>;

  const shown = data.people.filter((person) => person.ring[span] != null);
  const byRing = RING_NAMES.map((_, ring) => shown.filter((person) => person.ring[span] === ring));
  const communities = new Map(data.communities.map((community) => [community.id, community]));
  const current = active ? people.get(active) : undefined;
  const at = current ? placements.get(current.profile_id) : undefined;
  const open = (person: OrbitPerson) => router.push(`/profiles/${person.profile_id}`);
  const choose = (person: OrbitPerson) => hover ? open(person) : setActive(person.profile_id);

  return <div className={styles.layout}>
    <div className={styles.stage}>
      <div className={styles.toolbar}>
        <Segmented size="sm" label="统计时段" value={span} onChange={setSpan} options={WINDOWS} />
        <span className={styles.count}>{shown.length} 人 · 按私聊和通话排序</span>
      </div>
      <div className={styles.canvas} onPointerLeave={() => hover && setActive(null)}>
        <svg viewBox={`0 0 ${ORBIT_SIZE} ${ORBIT_SIZE}`} role="img" aria-label={`亲疏圈，${WINDOWS.find((w) => w.value === span)!.label}`}>
          <defs><clipPath id="orbit-avatar" clipPathUnits="objectBoundingBox"><circle cx=".5" cy=".5" r=".5" /></clipPath></defs>
          {[...RING_EDGES].reverse().map((edge, index) => <circle key={edge} className={styles.ring} data-ring={RING_EDGES.length - 1 - index} cx={ORBIT_SIZE / 2} cy={ORBIT_SIZE / 2} r={edge} />)}
          {RING_EDGES.map((edge, ring) => <text key={edge} className={styles.ringLabel} x={ORBIT_SIZE / 2} y={ORBIT_SIZE / 2 - edge + 18}>
            {RING_NAMES[ring]} · {data.rings[ring]}
          </text>)}
          <circle className={styles.self} cx={ORBIT_SIZE / 2} cy={ORBIT_SIZE / 2} r={20} />
          <text className={styles.selfLabel} x={ORBIT_SIZE / 2} y={ORBIT_SIZE / 2 + 6}>我</text>
          {data.people.map((person) => {
            const place = placements.get(person.profile_id)!;
            const dimmed = (focus && person.community !== focus) || place.ring == null;
            const avatar = place.ring != null && place.ring <= 1 && person.avatar_media_id;
            return <a key={person.profile_id} href={`/profiles/${person.profile_id}`} className={styles.person}
              aria-label={`${person.name}，${place.ring == null ? "不在这个时段的圈内" : RING_NAMES[place.ring]}`}
              tabIndex={place.ring == null ? -1 : 0} aria-hidden={place.ring == null || undefined}
              data-hidden={place.ring == null ? "" : undefined} data-dim={dimmed ? "" : undefined} data-active={active === person.profile_id ? "" : undefined}
              style={{ transform: `translate(${place.x}px, ${place.y}px)`, "--recency": recencyOpacity(person.last_contact_at) } as CSSProperties}
              onClick={(event) => { event.preventDefault(); choose(person); }}
              onPointerEnter={() => hover && setActive(person.profile_id)} onFocus={() => setActive(person.profile_id)}>
              <circle r={place.r} fill={colors.get(person.community)} className={styles.dot} />
              {avatar && <image href={localMediaUrl(avatar)} x={-place.r + 2.5} y={-place.r + 2.5} width={(place.r - 2.5) * 2} height={(place.r - 2.5) * 2}
                clipPath="url(#orbit-avatar)" preserveAspectRatio="xMidYMid slice" />}
              {place.ring != null && place.ring <= 1 && <text className={styles.name} y={place.r + 16}>{orbitLabel(person.name)}</text>}
            </a>;
          })}
        </svg>
        {current && at && at.ring != null && <div className={styles.tip} role="status"
          style={{ insetInlineStart: `${(at.x / ORBIT_SIZE) * 100}%`, insetBlockStart: `${(at.y / ORBIT_SIZE) * 100}%` }}>
          <strong>{current.name}</strong>
          <span>{communities.get(current.community)?.label} · {RING_NAMES[at.ring]}</span>
          <span>近 3 个月 {formatCount(current.messages["3"])} 条 · 近 12 个月 {formatCount(current.messages["12"])} 条{current.calls ? ` · ${current.calls} 次通话` : ""}</span>
          <span>上次联系 {relativeDays(current.last_contact_at)}</span>
          {!hover && <Link href={`/profiles/${current.profile_id}`}>打开资料</Link>}
        </div>}
      </div>
    </div>

    <aside className={styles.aside}>
      <section>
        <h3>圈子</h3>
        <CommunityLegend communities={data.communities} colors={colors} focus={focus} onFocus={setFocus} />
      </section>
      {byRing.slice(0, 2).map((members, ring) => members.length > 0 && <section key={ring}>
        <h3>{RING_NAMES[ring]}<small>{members.length} 人</small></h3>
        <ul className={styles.members}>{members.map((person) => <li key={person.profile_id}>
          <Link href={`/profiles/${person.profile_id}`} onPointerEnter={() => hover && setActive(person.profile_id)}>
            <Avatar name={person.name} mediaId={person.avatar_media_id} size={28} round />
            <span className={styles.memberName}>{person.name}</span>
            <small title={exactCount(person.messages[span])}>{formatCount(person.messages[span])}</small>
          </Link>
        </li>)}</ul>
      </section>)}
      <p className={styles.note}>{byRing[2].length} 位朋友、{byRing[3].length} 位熟人在外圈。颜色越淡，越久没联系。</p>
    </aside>
  </div>;
}
