import type { Directory, DirectoryFilters, DirectoryProfile, DirectorySort, IndexedPost } from "./directory";

type Pending = { resolve: (value: unknown) => void; reject: (reason: Error) => void; cleanup: () => void };
class SearchClient {
  worker: Worker | undefined;
  next = 0;
  version = 0;
  directory: Directory | undefined;
  pending = new Map<number, Pending>();
  constructor(readonly kind: "people" | "posts") {}
  start() {
    if (this.worker) return this.worker;
    const worker = new Worker(new URL("./search.worker.ts", import.meta.url));
    worker.onmessage = ({ data }) => {
      const pending = this.pending.get(data.id);
      if (!pending) return;
      this.pending.delete(data.id);
      pending.cleanup();
      if (data.error) pending.reject(new Error(data.error));
      else pending.resolve(data.result);
    };
    worker.onerror = () => this.reset();
    this.worker = worker;
    return worker;
  }
  request<T>(payload: object, signal?: AbortSignal): Promise<T> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(new DOMException("Cancelled", "AbortError")); return; }
      const worker = this.start();
      const id = ++this.next;
      const abort = () => {
        this.pending.delete(id);
        reject(new DOMException("Cancelled", "AbortError"));
      };
      signal?.addEventListener("abort", abort, { once: true });
      this.pending.set(id, { resolve: value => resolve(value as T), reject,
        cleanup: () => signal?.removeEventListener("abort", abort) });
      worker.postMessage({ ...payload, id, kind: this.kind });
    });
  }
  reset() {
    this.worker?.terminate();
    this.worker = undefined;
    this.directory = undefined;
    for (const pending of this.pending.values()) { pending.cleanup(); pending.reject(new Error("搜索暂时不可用，请重试")); }
    this.pending.clear();
  }
}
const people = new SearchClient("people");
const posts = new SearchClient("posts");

export async function searchPeople(directory: Directory, text: string, filters: DirectoryFilters,
  sort: DirectorySort, limit?: number, signal?: AbortSignal): Promise<DirectoryProfile[]> {
  const worker = people.start();
  if (people.directory !== directory) {
    people.directory = directory;
    worker.postMessage({ kind: "directory", directory, version: ++people.version });
  }
  const ids = await people.request<string[]>({ text, filters, sort, limit, version: people.version }, signal);
  return ids.map(id => directory.byId.get(id)).filter((p): p is DirectoryProfile => !!p);
}

export function searchPosts(text: string, limit: number, signal?: AbortSignal) {
  return posts.request<IndexedPost[]>({ text, limit }, signal);
}

export function resetSearchWorkers() { people.reset(); posts.reset(); }

let nextDirectoryVersion = 0;
const directoryVersions = new WeakMap<Directory, number>();
export function directoryVersion(directory?: Directory) {
  if (!directory) return 0;
  let version = directoryVersions.get(directory);
  if (!version) { version = ++nextDirectoryVersion; directoryVersions.set(directory, version); }
  return version;
}
