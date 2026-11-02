"use client";

import { Download, FileText, Paperclip, Upload, X } from "lucide-react";
import { useEffect, useState } from "react";

import { apiFetch } from "../lib/api";
import { localMediaUrl } from "../lib/media";
import styles from "./media-attachments.module.css";
import { Button } from "./ui/button";
import { Failure, Section } from "./ui/primitives";

export type Attachment = { id: string; media_type: string; width?: number; height?: number; byte_length?: number; file_name?: string | null };

const MAX_FILES = 50;
const MAX_BYTES = 500 * 1024 * 1024;
// The types the API verifies and the browser can show inline; everything else is listed as a file to download.
const VISUAL = new Set(["image/jpeg", "image/png", "image/webp", "image/gif", "video/mp4", "video/quicktime", "video/webm"]);
const isVisual = (item: Attachment) => VISUAL.has(item.media_type);

function fileSize(bytes?: number) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  const power = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** power).toFixed(power ? 1 : 0)} ${units[power]}`;
}

function fileLabel(item: Attachment) {
  return item.file_name || `${item.media_type.split("/")[1]?.toUpperCase() || "文件"} 文件`;
}

export function AttachmentList({ items, onRemove }: { items: Attachment[]; onRemove?: (id: string) => void }) {
  const visual = items.filter(isVisual);
  const files = items.filter((item) => !isVisual(item));
  if (!items.length) return null;
  return <div className={styles.attachments}>
    {visual.length > 0 && <div className={styles.grid}>{visual.map((item) => <figure key={item.id}>
      {item.media_type.startsWith("video/")
        ? <video controls preload="metadata" src={localMediaUrl(item.id)} />
        : <a href={localMediaUrl(item.id)} target="_blank" rel="noopener noreferrer" aria-label="查看照片原图"><img src={localMediaUrl(item.id)} alt="上传的照片" loading="lazy" /></a>}
      {onRemove && <button type="button" className={styles.remove} onClick={() => onRemove(item.id)} aria-label="移除附件" title="移除"><X size={13} /></button>}
    </figure>)}</div>}
    {files.length > 0 && <ul className={styles.files}>{files.map((item) => <li key={item.id}>
      <FileText size={16} aria-hidden="true" />
      <a href={localMediaUrl(item.id)} target="_blank" rel="noopener noreferrer" title={fileLabel(item)}>
        <span>{fileLabel(item)}</span><small>{fileSize(item.byte_length)}</small>
      </a>
      <a className={styles.fileAction} href={localMediaUrl(item.id)} download={item.file_name || true} aria-label={`下载 ${fileLabel(item)}`} title="下载"><Download size={14} /></a>
      {onRemove && <button type="button" className={styles.fileAction} onClick={() => onRemove(item.id)} aria-label={`移除 ${fileLabel(item)}`} title="移除"><X size={14} /></button>}
    </li>)}</ul>}
  </div>;
}

export function AttachmentPicker({ items, onChange, onBusy }: { items: Attachment[]; onChange: (items: Attachment[]) => void; onBusy?: (busy: boolean) => void }) {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  async function upload(files: File[]) {
    if (!files.length) return;
    if (items.length + files.length > MAX_FILES) {
      setError(`一次最多保存 ${MAX_FILES} 个附件。`);
      return;
    }
    setBusy(true);
    onBusy?.(true);
    setError("");
    const added = [...items];
    try {
      for (const [index, file] of files.entries()) {
        if (file.size > MAX_BYTES) throw new Error(`${file.name} 超过 500 MB。`);
        setStatus(`正在上传 ${index + 1}/${files.length}：${file.name}`);
        const form = new FormData();
        form.append("file", file);
        const response = await apiFetch("/api/v1/media", { method: "POST", body: form });
        if (!response.ok) throw new Error(response.status === 413 ? `${file.name} 超过 500 MB。`
          : response.status === 422 ? `${file.name} 看起来是照片或视频，但文件已损坏，无法读取。` : `${file.name} 上传失败，请重试。`);
        const media: Attachment = await response.json();
        if (!added.some((old) => old.id === media.id)) added.push(media);
        onChange([...added]);
      }
      setStatus("上传完成，请保存。");
    } catch (reason) {
      setStatus("");
      setError(reason instanceof Error ? reason.message : "上传失败，已上传的附件已保留。");
    } finally {
      setBusy(false);
      onBusy?.(false);
    }
  }
  return <div className={styles.picker}>
    <label className={styles.drop} data-busy={busy ? "" : undefined}>
      <Upload size={16} aria-hidden="true" />选择或拖入照片、视频、文件
      <input type="file" multiple disabled={busy}
        onChange={(event) => { void upload(Array.from(event.target.files || [])); event.target.value = ""; }} />
    </label>
    <small>PDF、文档、表格等任意文件都可以；每个最大 500 MB，最多 {MAX_FILES} 个。</small>
    {status && <p role="status">{status}</p>}
    {error && <p role="alert">{error}</p>}
    <AttachmentList items={items} onRemove={busy ? undefined : (id) => onChange(items.filter((item) => item.id !== id))} />
  </div>;
}

/** Photos and files kept about this person by hand. */
export function ProfilePhotos({ profileId }: { profileId: string }) {
  const [items, setItems] = useState<Attachment[]>([]);
  const [draft, setDraft] = useState<Attachment[]>([]);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  async function load() {
    const response = await fetch(`/api/v1/profiles/${profileId}/photos`, { cache: "no-cache" });
    if (!response.ok) throw new Error(String(response.status));
    setItems(await response.json());
  }
  useEffect(() => {
    setItems([]);
    setDraft([]);
    setEditing(false);
    void load().catch(() => setError("照片和文件读取失败。"));
  }, [profileId]);
  async function run(action: () => Promise<Response>, failure: string) {
    setBusy(true);
    setError("");
    try {
      const response = await action();
      if (!response.ok) throw new Error(String(response.status));
      await load();
      return true;
    } catch {
      setError(failure);
      return false;
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    const saved = await run(() => apiFetch(`/api/v1/profiles/${profileId}/photos`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ media_ids: draft.map((item) => item.id) }),
    }), "保存失败，已上传的附件仍保留，请重试。");
    if (saved) {
      setDraft([]);
      setEditing(false);
    }
  }
  return <Section title="照片与文件" icon={<Paperclip size={15} aria-hidden="true" />}
    action={!editing && <Button variant="ghost" size="sm" onClick={() => setEditing(true)}><Upload size={13} />上传</Button>}>
    {error && <Failure>{error}</Failure>}
    <AttachmentList items={items} onRemove={busy ? undefined : (id) => void run(() => apiFetch(`/api/v1/profiles/${profileId}/photos/${id}`, { method: "DELETE" }), "移除失败，请重试。")} />
    {editing && <div className={styles.editor}>
      <AttachmentPicker items={draft} onChange={setDraft} onBusy={setUploading} />
      <div className={styles.actions}>
        <Button size="sm" disabled={uploading || busy} onClick={() => setEditing(false)}>取消</Button>
        <Button size="sm" variant="primary" disabled={uploading || busy || !draft.length} onClick={() => void save()}>{busy ? "保存中…" : "保存"}</Button>
      </div>
    </div>}
  </Section>;
}
