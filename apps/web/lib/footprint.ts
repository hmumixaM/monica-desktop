import { primarySource } from "./map";

export type AtlasPerson = { id: string; name: string };
export type AtlasEvent = { at: string; text: string; source: string; url: string };
export type AtlasPoint = {
  label: string; latitude: number; longitude: number; count: number; sources: string[]; source_kind: string; precision: string;
  people_count: number; people: AtlasPerson[]; events: AtlasEvent[]; months?: Record<string, number>; places?: AtlasPoint[];
};
export type Atlas = {
  points: AtlasPoint[]; people: number; place_count: number; located_records: number; post_records: number; profile_records: number;
  years: number[]; months: string[];
  unresolved: { label: string; count: number; sources: string[]; people: AtlasPerson[] }[];
};

export function collection(points: AtlasPoint[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: points.map((point, index) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [point.longitude, point.latitude] },
      properties: { i: index, label: point.label, source: primarySource(point.sources) },
    })),
  };
}

/** One clicked cluster as a single place that lists what it holds, busiest place first. */
export function area(group: AtlasPoint[]): AtlasPoint {
  const places = [...group].sort((a, b) => b.count - a.count || b.people_count - a.people_count);
  const people = [...new Map(places.flatMap((place) => place.people).map((person) => [person.id, person])).values()];
  const events = group.flatMap((place) => place.events).sort((a, b) => b.at.localeCompare(a.at));
  return {
    ...places[0], label: places.length > 1 ? `${places[0].label} 等 ${places.length} 个地点` : places[0].label,
    places, people, people_count: people.length, events, count: group.reduce((sum, place) => sum + place.count, 0),
    source_kind: "mixed", sources: [...new Set(group.flatMap((place) => place.sources))],
  };
}

/** For each point, the indexes into `months` it has records in, ascending. */
export function monthIndexes(points: AtlasPoint[], months: string[]) {
  const index = new Map(months.map((month, at) => [month, at]));
  return points.map((point) => Object.keys(point.months ?? {}).map((month) => index.get(month)!).sort((a, b) => a - b));
}

/** Footprints up to one month: that month's places lit, earlier ones fading with age. */
export function trail(points: AtlasPoint[], indexes: number[][], current: number): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = [];
  points.forEach((point, i) => {
    const seen = indexes[i];
    let last = -1;
    for (const at of seen) {
      if (at > current) break;
      last = at;
    }
    if (last < 0) return;
    const now = last === current;
    features.push({
      type: "Feature", geometry: { type: "Point", coordinates: [point.longitude, point.latitude] },
      properties: { i, label: point.label, source: primarySource(point.sources), now, fade: now ? 1 : Math.max(0.14, 0.5 - (current - last) / 48) },
    });
  });
  features.sort((a, b) => Number(a.properties!.now) - Number(b.properties!.now));
  return { type: "FeatureCollection", features };
}

export function monthLabel(month: string) {
  const [year, value] = month.split("-");
  return `${year} 年 ${Number(value)} 月`;
}
