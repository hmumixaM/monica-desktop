export type OrbitWindow = "3" | "12";

export interface OrbitPerson {
  profile_id: string;
  name: string;
  avatar_media_id?: string | null;
  community: string;
  last_contact_at?: string | null;
  calls: number;
  messages: Record<OrbitWindow, number>;
  ring: Record<OrbitWindow, number | null>;
}

/** `label_source` is set when the label was typed by hand or suggested by the model. */
export interface OrbitCommunity { id: string; label: string; size: number; label_source?: "manual" | "model" }

export interface Orbit {
  rings: number[];
  windows: number[];
  communities: OrbitCommunity[];
  people: OrbitPerson[];
}

export interface Placement { x: number; y: number; r: number; ring: number | null }

/** The SVG is drawn in a square of this size; everything below is in its units. */
export const ORBIT_SIZE = 1000;
const CENTER = ORBIT_SIZE / 2;
/** Outer edge of each ring, and where its people sit. */
export const RING_EDGES = [150, 255, 365, 475];
const RING_MIDDLES = [92, 202, 310, 420];
const COMMUNITY_GAP = 0.05;
const PADDING = 3;

export const ringRadius = (ring: number) => RING_MIDDLES[ring];

const LABEL_CHARS = 7;
/** Inner-ring names are drawn under their dots; this is the text shown. */
export const orbitLabel = (name: string) => [...name].length > LABEL_CHARS ? `${[...name].slice(0, LABEL_CHARS - 1).join("")}…` : name;
/** Rough width of a label at the 16-unit font: wide for CJK and emoji, narrow for Latin. */
const labelWidth = (name: string) => [...orbitLabel(name)].reduce((sum, char) => sum + (char.charCodeAt(0) > 0x2e7f ? 16 : 9), 0);

/**
 * Where each person sits for one window. Angle is their community's sector, radius their ring;
 * people sharing a ring are then pushed apart along it so dots do not overlap.
 */
export function placeOrbit(data: Orbit, window: OrbitWindow): Map<string, Placement> {
  const total = data.communities.reduce((sum, community) => sum + community.size, 0) || 1;
  const usable = Math.PI * 2 - COMMUNITY_GAP * data.communities.length;
  const sectors = new Map<string, [number, number]>();
  let cursor = -Math.PI / 2;
  for (const community of data.communities) {
    const width = (community.size / total) * usable;
    sectors.set(community.id, [cursor, cursor + width]);
    cursor += width + COMMUNITY_GAP;
  }

  const peak = Math.max(1, ...data.people.map((person) => person.messages[window]));
  const size = (person: OrbitPerson, ring: number) => {
    const base = ring <= 1 ? 13 : 5;
    return base + (ring <= 1 ? 9 : 10) * Math.sqrt(person.messages[window] / peak);
  };

  const placements = new Map<string, Placement>();
  for (let ring = 0; ring < RING_MIDDLES.length; ring += 1) {
    const radius = RING_MIDDLES[ring];
    const members = data.people.filter((person) => person.ring[window] === ring);
    const grouped = new Map<string, OrbitPerson[]>();
    for (const person of members) grouped.set(person.community, [...(grouped.get(person.community) ?? []), person]);
    const items: { person: OrbitPerson; angle: number; r: number; label: number; half: number }[] = [];
    for (const [community, people] of grouped) {
      const [start, end] = sectors.get(community) ?? [0, Math.PI * 2];
      people.forEach((person, index) => {
        const r = size(person, ring);
        items.push({ person, r, label: ring <= 1 ? labelWidth(person.name) : 0, half: r, angle: start + ((index + 0.5) / people.length) * (end - start) });
      });
    }
    // An inner-ring dot carries its name underneath, so it needs the box's extent along the ring at its angle.
    const extent = (item: (typeof items)[number]) => item.label
      ? Math.max(item.r, (item.label / 2) * Math.abs(Math.sin(item.angle)) + (item.r + 11) * Math.abs(Math.cos(item.angle)))
      : item.r;
    for (let round = 0; round < 3; round += 1) {
      for (const item of items) item.half = extent(item);
      spread(items, radius);
    }
    for (const item of items) {
      placements.set(item.person.profile_id, {
        x: CENTER + Math.cos(item.angle) * radius, y: CENTER + Math.sin(item.angle) * radius, r: item.r, ring,
      });
    }
  }
  for (const person of data.people) {
    if (placements.has(person.profile_id)) continue;
    const [start, end] = sectors.get(person.community) ?? [0, 0];
    const angle = (start + end) / 2;
    placements.set(person.profile_id, {
      x: CENTER + Math.cos(angle) * (ORBIT_SIZE / 2 + 40), y: CENTER + Math.sin(angle) * (ORBIT_SIZE / 2 + 40), r: 4, ring: null,
    });
  }
  return placements;
}

/** Relaxes angles on one circle until neighbours no longer overlap, keeping their order. */
function spread(items: { angle: number; half: number }[], radius: number) {
  if (items.length < 2) return;
  items.sort((a, b) => a.angle - b.angle);
  const gap = (a: { half: number }, b: { half: number }) => (a.half + b.half + PADDING) / radius;
  const needed = items.reduce((sum, item, index) => sum + gap(item, items[(index + 1) % items.length]), 0);
  const scale = needed > Math.PI * 2 ? (Math.PI * 2) / needed : 1;
  for (let pass = 0; pass < 80; pass += 1) {
    let moved = false;
    for (let index = 0; index < items.length; index += 1) {
      const a = items[index];
      const b = items[(index + 1) % items.length];
      const wrap = index === items.length - 1 ? Math.PI * 2 : 0;
      const deficit = gap(a, b) * scale - (b.angle + wrap - a.angle);
      if (deficit > 1e-4) {
        a.angle -= deficit / 2;
        b.angle += deficit / 2;
        moved = true;
      }
    }
    if (!moved) break;
  }
}

/** Faded by how long ago you last talked. */
export function recencyOpacity(last?: string | null, now = Date.now()) {
  if (!last) return 0.3;
  const days = (now - new Date(last).getTime()) / 86_400_000;
  return days < 30 ? 1 : days < 90 ? 0.8 : days < 365 ? 0.55 : 0.35;
}
