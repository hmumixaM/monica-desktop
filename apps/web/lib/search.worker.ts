import MiniSearch from "minisearch";
import { get, set } from "idb-keyval";
import type { Directory, IndexedPost } from "./directory";
import type { RawPostIndex } from "./api-types";
import { searchDirectory, tokenize } from "./search-engine";

let directory: Directory | undefined;
let version = 0;
let posts: { search: MiniSearch<IndexedPost>; byId: Map<string, IndexedPost> } | undefined;
let loading: Promise<void> | undefined;
let checkedAt = 0;
const STORE = "monica.directory-posts.v1";

async function loadPosts() {
  if (posts && Date.now() - checkedAt < 300_000) return;
  if (loading) return loading;
  loading = (async () => {
    const cached = await get<{ etag: string | null; data: RawPostIndex }>(STORE).catch(() => undefined);
    const response = await fetch("/api/v1/directory/posts", { credentials: "same-origin", cache: "no-cache",
      headers: cached?.etag ? { "If-None-Match": cached.etag } : {} });
    if (!response.ok && response.status !== 304) throw new Error("动态搜索加载失败，请重试");
    const snapshot = response.status === 304 && cached ? cached : { etag: response.headers.get("ETag"), data: await response.json() as RawPostIndex };
    if (!posts || response.status !== 304) {
      const rows: IndexedPost[] = snapshot.data.posts.map(([id, profileId, provider, occurredAt, text]) => ({ id, profileId, provider, occurredAt, text }));
      const search = new MiniSearch<IndexedPost>({ fields: ["text"], storeFields: ["id"], tokenize,
        searchOptions: { prefix: true, combineWith: "AND" } });
      search.addAll(rows);
      posts = { search, byId: new Map(rows.map(row => [row.id, row])) };
      await set(STORE, snapshot).catch(() => undefined);
    }
    checkedAt = Date.now();
  })().finally(() => { loading = undefined; });
  return loading;
}

self.onmessage = async ({ data }) => {
  if (data.kind === "directory") { directory = data.directory; version = data.version; return; }
  try {
    let result: unknown;
    if (data.kind === "people") {
      if (!directory || data.version !== version) throw new Error("联系人已更新，请重试");
      result = searchDirectory(directory, data.text, data.filters, data.sort, data.limit).map(p => p.id);
    } else {
      await loadPosts();
      result = posts!.search.search(data.text).slice(0, data.limit).map(hit => posts!.byId.get(String(hit.id))!);
    }
    self.postMessage({ id: data.id, result });
  } catch (error) {
    self.postMessage({ id: data.id, error: error instanceof Error ? error.message : "搜索失败" });
  }
};
