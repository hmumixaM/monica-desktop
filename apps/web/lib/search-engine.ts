import MiniSearch, { type SearchResult } from "minisearch";
import { pinyin } from "pinyin-pro";
import type { Directory, DirectoryProfile, DirectoryFilters, DirectorySort } from "./directory";

const HAN = /\p{Script=Han}/u;

/** Words plus, for Chinese, single characters and adjacent pairs. */
export function tokenize(value: string): string[] {
  const tokens: string[] = [];
  for (const word of value.toLowerCase().split(/[\s,，·、/|()（）\-_.@]+/u)) {
    if (!word) continue;
    tokens.push(word);
    if (HAN.test(word)) {
      const chars = [...word];
      chars.forEach((char, index) => {
        tokens.push(char);
        if (index + 1 < chars.length) tokens.push(char + chars[index + 1]);
      });
    }
  }
  return tokens;
}

interface SearchDeps { MiniSearch: typeof MiniSearch; pinyin: typeof pinyin }
const searchDeps: SearchDeps = { MiniSearch, pinyin };

function romanized(deps: SearchDeps, value: string): string {
  if (!HAN.test(value)) return "";
  const full = deps.pinyin(value, { toneType: "none", type: "array", surname: "head" }).join("");
  const initials = deps.pinyin(value, { pattern: "first", toneType: "none", type: "array", surname: "head" }).join("");
  return `${full} ${initials}`;
}

interface IndexedProfile {
  id: string;
  name: string;
  romanized: string;
  aliases: string;
  groupNames: string;
  headline: string;
  city: string;
}

const indexes = new WeakMap<Directory, MiniSearch<IndexedProfile>>();

function searchIndex(directory: Directory, deps: SearchDeps): MiniSearch<IndexedProfile> {
  let index = indexes.get(directory);
  if (!index) {
    index = new deps.MiniSearch<IndexedProfile>({
      fields: ["name", "romanized", "aliases", "groupNames", "headline", "city"],
      tokenize,
      searchOptions: { prefix: true, fuzzy: 0.15, combineWith: "AND", boost: { name: 3, romanized: 2, aliases: 2 } },
    });
    index.addAll(directory.profiles.map((profile) => ({
      id: profile.id,
      name: profile.name,
      romanized: [profile.name, ...profile.aliases, ...profile.groupNames].map((value) => romanized(deps, value)).join(" "),
      aliases: profile.aliases.join(" "),
      groupNames: profile.groupNames.join(" "),
      headline: profile.headline ?? "",
      city: profile.city ?? "",
    })));
    indexes.set(directory, index);
  }
  return index;
}

function matches(profile: DirectoryProfile, filters: DirectoryFilters): boolean {
  return (!filters.type || profile.type === filters.type)
    && (filters.tagIds ?? []).every((tag) => profile.tagIds.includes(tag))
    && (!filters.companyId || profile.companyIds.includes(filters.companyId))
    && (!filters.schoolId || profile.schoolIds.includes(filters.schoolId))
    && (!filters.provider || profile.providers.includes(filters.provider));
}

function daysUntilBirthday(md: string | null, today: Date): number {
  if (!md) return Number.POSITIVE_INFINITY;
  const [month, day] = md.split("-").map(Number);
  const next = new Date(today.getFullYear(), month - 1, day);
  if (next < new Date(today.getFullYear(), today.getMonth(), today.getDate())) next.setFullYear(today.getFullYear() + 1);
  return Math.round((next.getTime() - today.getTime()) / 86_400_000);
}

const collator = new Intl.Collator("zh-Hans-CN", { sensitivity: "base", numeric: true });

function sortProfiles(profiles: DirectoryProfile[], sort: DirectorySort): DirectoryProfile[] {
  const today = new Date();
  const compare: Record<Exclude<DirectorySort, "relevance">, (a: DirectoryProfile, b: DirectoryProfile) => number> = {
    name: (a, b) => collator.compare(a.name, b.name),
    last_contact: (a, b) => (b.lastContactAt ?? "").localeCompare(a.lastContactAt ?? ""),
    messages: (a, b) => (b.directMessageCount ?? 0) - (a.directMessageCount ?? 0),
    birthday: (a, b) => daysUntilBirthday(a.birthdayMd, today) - daysUntilBirthday(b.birthdayMd, today),
  };
  return sort === "relevance" ? profiles : [...profiles].sort(compare[sort]);
}

export function searchDirectory(directory: Directory, text: string, filters: DirectoryFilters = {},
                                sort: DirectorySort = "relevance", limit?: number, deps = searchDeps): DirectoryProfile[] {
  const query = text.trim();
  let results: DirectoryProfile[];
  if (!query) {
    results = directory.profiles.filter((profile) => matches(profile, filters));
    results = sortProfiles(results, sort === "relevance" ? "name" : sort);
  } else if (!deps) {
    const needle = query.toLowerCase();
    results = sortProfiles(directory.profiles.filter((profile) => matches(profile, filters)
      && [profile.name, ...profile.aliases, ...profile.groupNames].some((value) => value.toLowerCase().includes(needle))), sort);
  } else {
    const hits: SearchResult[] = searchIndex(directory, deps).search(query, {
      filter: (hit) => matches(directory.byId.get(hit.id as string)!, filters),
    });
    results = sortProfiles(hits.map((hit) => directory.byId.get(hit.id as string)!), sort);
  }
  return limit ? results.slice(0, limit) : results;
}
