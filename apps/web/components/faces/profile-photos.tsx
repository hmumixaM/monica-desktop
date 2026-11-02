"use client";

import { type InfiniteData, useQueryClient } from "@tanstack/react-query";
import { Images, ScanFace } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { type FaceDecision, type FaceState, useDecideFaces, useRestoreFaces } from "../../lib/faces";
import { localMediaUrl } from "../../lib/media";
import { type PhotoFace, type PhotoPage, type PhotoSort, type PhotoState, type ProfilePhoto,
  photoKey, usePhotoReading, usePhotoStatus, useProfilePhotos } from "../../lib/profile-photos";
import { fetchJson } from "../../lib/queries";
import { Button } from "../ui/button";
import { Dialog } from "../ui/dialog";
import { LoadMore } from "../ui/load-more";
import { PersonPicker } from "../ui/person-picker";
import { EmptyState, Failure, formatDateTime, Loading } from "../ui/primitives";
import { Segmented } from "../ui/segmented";
import styles from "./profile-photos.module.css";

type GalleryView = "faces" | "photos";
type Selection = { photo: ProfilePhoto; faceId?: string };
const preferences = new Map<string, { state: PhotoState; sort: PhotoSort; view: GalleryView }>();
const positions = new Map<string, number>();
const stateLabel = { all: "全部", pending: "候选", confirmed: "已确认" };
const providerLabel: Record<string, string> = { wechat: "微信", instagram: "Instagram", linkedin: "LinkedIn" };
const BATCH_LIMIT = 200;

function FaceSelection({ ids, selected, busy, label, onToggle }: {
  ids: string[]; selected: Set<string>; busy: boolean; label: string; onToggle: (ids: string[]) => void;
}) {
  const count = ids.filter((id) => selected.has(id)).length;
  return <label className={styles.selection} title={label}>
    <input type="checkbox" aria-label={label} checked={!!ids.length && count === ids.length}
      ref={(input) => { if (input) input.indeterminate = count > 0 && count < ids.length; }}
      disabled={busy || !ids.length} onChange={() => onToggle(ids)} />
    <span>选择</span>
  </label>;
}

function FaceThumbnail({ photo, face }: { photo: ProfilePhoto; face: PhotoFace }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!failed || attempt >= 3) return;
    const timer = window.setTimeout(() => { setAttempt((value) => value + 1); setFailed(false); }, 1500 * (attempt + 1));
    return () => window.clearTimeout(timer);
  }, [failed, attempt]);
  return <span className={styles.faceImage}>
    {failed ? <span className={styles.faceMissing}>预览暂不可用</span>
      : <img key={attempt} src={`${localMediaUrl(photo.media_id)}/preview?face_id=${face.id}&v=${encodeURIComponent(face.box.join(","))}`}
        alt="人脸预览" loading="lazy" decoding="async" onError={() => setFailed(true)} />}
  </span>;
}

function sourceLabel(source: ProfilePhoto["sources"][number]) {
  const platform = providerLabel[source.provider ?? ""] || source.provider || "";
  const kind = source.entity_type === "social_story" ? "Story" : source.entity_type === "message_event"
    ? source.conversation_type === "group" ? "群聊" : "聊天" : source.entity_type === "social_post" ? "动态" : "照片";
  return `${platform} ${kind}`.trim();
}

/** The image and its normalized boxes share one aspect ratio, including EXIF orientation. */
function PhotoImage({ photo, original = false, selected, onSelect }: {
  photo: ProfilePhoto; original?: boolean; selected?: string; onSelect?: (id: string) => void;
}) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [ratio, setRatio] = useState(photo.width && photo.height ? photo.width / photo.height : 1);
  useEffect(() => {
    if (!failed || original || attempt >= 3) return;
    const timer = window.setTimeout(() => { setAttempt((value) => value + 1); setFailed(false); }, 1500 * (attempt + 1));
    return () => window.clearTimeout(timer);
  }, [failed, original, attempt]);
  const src = `${localMediaUrl(photo.media_id)}${original ? "" : "/preview"}`;
  return <div className={styles.image} style={{ aspectRatio: ratio, ...(original ? { width: `min(100%, ${72 * ratio}vh)` } : {}) }}>
    {!failed ? <>
      <img key={attempt} src={src} alt={original ? "完整照片" : "相关照片预览"} loading={original ? "eager" : "lazy"}
        decoding="async" onLoad={(e) => setRatio(e.currentTarget.naturalWidth / e.currentTarget.naturalHeight)} onError={() => setFailed(true)} />
      {photo.faces.map((face) => {
        const style = { left: `${face.box[0] * 100}%`, top: `${face.box[1] * 100}%`, width: `${face.box[2] * 100}%`, height: `${face.box[3] * 100}%` };
        return onSelect ? <button key={face.id} type="button" className={styles.box} style={style}
          data-selected={selected === face.id} data-status={face.status} aria-pressed={selected === face.id}
          aria-label={`选择人脸：${face.name || "未标注"}，${stateLabel[face.status]}`} onClick={() => onSelect(face.id)} />
          : <span key={face.id} className={styles.box} style={style} data-status={face.status} aria-hidden="true" />;
      })}
    </> : <div className={styles.missing}>{original ? "原图暂不可用" : "预览暂不可用"}
      {original && <Button size="sm" onClick={() => { setFailed(false); setAttempt(attempt + 1); }}>重试图片</Button>}
    </div>}
  </div>;
}

export function ProfilePhotoGallery({ profileId, name }: { profileId: string; name: string }) {
  const initial = preferences.get(profileId);
  const [state, setState] = useState<PhotoState>(initial?.state ?? "all");
  const [sort, setSort] = useState<PhotoSort>(initial?.sort ?? "time");
  const [view, setView] = useState<GalleryView>(initial?.view ?? "faces");
  const [selected, setSelected] = useState<Selection | null>(null);
  const [batchMode, setBatchMode] = useState(false);
  const [selection, setSelection] = useState(new Set<string>());
  const [batchNotice, setBatchNotice] = useState("");
  const [batchError, setBatchError] = useState("");
  const [batchUndo, setBatchUndo] = useState<{ states: FaceState[]; snapshots: [readonly unknown[], unknown][] } | null>(null);
  const decideBatch = useDecideFaces();
  const restoreBatch = useRestoreFaces();
  const batchBusy = decideBatch.isPending || restoreBatch.isPending;
  const [linkError, setLinkError] = useState(false);
  const params = useSearchParams();
  const opened = useRef<string | null>(null);
  const client = useQueryClient();
  const photos = useProfilePhotos(profileId, state, sort);
  const status = usePhotoStatus(profileId, photos.isSuccess);
  const key = JSON.stringify([...photoKey(profileId, state, sort), view]);
  const panel = useRef<HTMLDivElement>(null);
  const rows = [...new Map((photos.data?.pages.flatMap((page) => page.items) ?? []).map((photo) => [photo.media_id, photo])).values()];
  const counts = status.data?.counts ?? photos.data?.pages[0]?.counts;
  const shownRevision = photos.data?.pages[0]?.revision;
  const changed = !!shownRevision && !!status.data && status.data.revision !== shownRevision;
  const loadingNext = useRef(false);
  const eligible = new Map(rows.flatMap((photo) => photo.faces
    .filter((face) => state !== "confirmed" && face.profile_id === profileId && face.status === "pending")).map((face) => [face.id, face]));
  const selectedIds = [...selection].filter((id) => eligible.has(id));
  const selectedFaces = new Set(selectedIds);
  const selectedPhotos = new Set(selectedIds.map((id) => eligible.get(id)!.media_id)).size;

  function clearBatch() { setSelection(new Set()); setBatchError(""); setBatchNotice(""); setBatchUndo(null); }

  function toggleFaces(ids: string[]) {
    const next = new Set(selectedIds);
    if (ids.every((id) => next.has(id))) ids.forEach((id) => next.delete(id));
    else ids.forEach((id) => { if (next.size < BATCH_LIMIT) next.add(id); });
    setSelection(next); setBatchError(""); setBatchNotice("");
  }

  async function confirmBatch() {
    if (!selectedIds.length || batchBusy) return;
    setBatchError("");
    try {
      const result = await decideBatch.mutateAsync({ face_ids: selectedIds, action: "confirm", profile_id: profileId });
      const snapshots = client.getQueriesData({ queryKey: ["profile-photo-gallery"] });
      const confirmedIds = new Set(result.previous.map((face) => face.id));
      setBatchUndo(result.previous.length ? { states: result.previous, snapshots } : null);
      for (const [query] of snapshots) {
        if (JSON.stringify(query) !== JSON.stringify(photoKey(profileId, state, sort))) {
          for (const view of ["faces", "photos"]) positions.delete(JSON.stringify([...query, view]));
          client.removeQueries({ queryKey: query, exact: true }); continue;
        }
        client.setQueryData<InfiniteData<PhotoPage, string | null>>(query, (old) => old && ({ ...old, pages: old.pages.map((page) => ({ ...page,
          items: page.items.flatMap((item) => {
            const faces = item.faces.map((face) => confirmedIds.has(face.id)
              ? { ...face, status: "confirmed" as const, profile_id: profileId, name } : face);
            const own = faces.filter((face) => face.profile_id === profileId);
            const pending = own.some((face) => face.status === "pending"), confirmed = own.some((face) => face.status === "confirmed");
            return state === "pending" && !pending ? [] : [{ ...item, faces, pending, confirmed }];
          }),
        })) }));
      }
      setSelection(new Set());
      setBatchNotice(`已确认 ${result.previous.length} 张人脸。后续匹配任务会读取本次反馈。`);
      await client.invalidateQueries({ queryKey: ["profile-photo-status"] });
    } catch { setBatchError("未能确认保存结果，所选人脸已保留，可重试。"); }
  }

  async function undoBatch() {
    if (!batchUndo || batchBusy) return;
    setBatchError("");
    try {
      await restoreBatch.mutateAsync(batchUndo.states);
      for (const [query, data] of batchUndo.snapshots) client.setQueryData(query, data);
      setBatchUndo(null); setSelection(new Set()); setBatchNotice("已撤销本次批量确认。");
      await client.invalidateQueries({ queryKey: ["profile-photo-status"] });
    } catch { setBatchError("未能确认撤销结果，可再次撤销。"); }
  }

  useLayoutEffect(() => {
    const y = positions.get(key);
    if (y != null) window.scrollTo({ top: y, behavior: "instant" });
    let last = window.scrollY;
    const remember = () => { last = window.scrollY; };
    window.addEventListener("scroll", remember, { passive: true });
    return () => { positions.set(key, last); window.removeEventListener("scroll", remember); };
  }, [key]);

  useEffect(() => { preferences.set(profileId, { state, sort, view }); }, [profileId, state, sort, view]);

  useEffect(() => {
    const media = params.get("media");
    if (!media || opened.current === media) return;
    let active = true;
    fetchJson<PhotoPage>(`/api/v1/faces/people/${profileId}/photos?media_id=${encodeURIComponent(media)}`)
      .then((page) => { if (active) { opened.current = media; setSelected(page.items[0] ? { photo: page.items[0] } : null); setLinkError(!page.items.length); } })
      .catch(() => { if (active) setLinkError(true); });
    return () => { active = false; };
  }, [params, profileId]);

  async function loadMore() {
    if (batchBusy || loadingNext.current || photos.isFetching || !photos.hasNextPage) return;
    loadingNext.current = true;
    try { await photos.fetchNextPage(); } finally { loadingNext.current = false; }
  }

  function refresh() {
    client.setQueryData<InfiniteData<PhotoPage, string | null>>(photoKey(profileId, state, sort), (old) => old && ({
      pages: old.pages.slice(0, 1), pageParams: old.pageParams.slice(0, 1),
    }));
    void photos.refetch();
    void status.refetch();
    panel.current?.scrollIntoView({ block: "start" });
  }

  return <div data-tab-panel="photos" ref={panel} className={styles.gallery}>
    <div className={styles.toolbar}>
      <Segmented label="照片归属状态" options={(["all", "pending", "confirmed"] as const).map((value) => ({ value, label: stateLabel[value], count: counts?.[value], disabled: batchBusy }))}
        value={state} onChange={(value) => { if (value !== state) { clearBatch(); setState(value); panel.current?.scrollIntoView({ block: "start" }); } }} />
      <Button size="sm" className={styles.batchToggle} aria-pressed={batchMode} disabled={batchBusy || (!batchMode && !rows.length && !batchUndo)}
        onClick={() => { setBatchMode(!batchMode); setSelection(new Set()); setBatchError(""); }}>
        {batchMode ? "退出批量管理" : "批量管理"}
      </Button>
      <div className={styles.viewControls}>
        <Segmented label="浏览视图" value={view} options={[
          { value: "faces", label: <><ScanFace size={16} aria-hidden="true" />人脸</> },
          { value: "photos", label: <><Images size={16} aria-hidden="true" />照片</> },
        ]} onChange={(value) => { setView(value); if (!positions.has(JSON.stringify([...photoKey(profileId, state, sort), value]))) panel.current?.scrollIntoView({ block: "start" }); }} />
        <label>排序 <select aria-label="照片排序" value={sort} disabled={batchBusy} onChange={(e) => { clearBatch(); setSort(e.target.value as PhotoSort); panel.current?.scrollIntoView({ block: "start" }); }}>
          <option value="time">最新照片</option><option value="score">匹配分数</option>
        </select></label>
      </div>
    </div>
    {batchMode && (!!rows.length || !!batchUndo || !!batchNotice) && <section className={styles.bulk} aria-label="批量确认" aria-busy={batchBusy}>
      <div className={styles.bulkSummary}><strong>{selectedIds.length ? `已选 ${selectedIds.length} 张人脸` : "批量确认"}</strong>
        {!!selectedIds.length && <span>来自 {selectedPhotos} 张照片</span>}</div>
      <div className={styles.actions}>
        <Button size="sm" disabled={batchBusy || !eligible.size} onClick={() => { setSelection(new Set([...eligible.keys()].slice(0, BATCH_LIMIT))); setBatchError(""); setBatchNotice(""); }}>选择已加载候选</Button>
        <Button size="sm" variant="ghost" disabled={batchBusy || !selectedIds.length} onClick={() => setSelection(new Set())}>清空选择</Button>
        <Button size="sm" variant="primary" disabled={batchBusy || !selectedIds.length} onClick={() => void confirmBatch()}>{decideBatch.isPending ? "正在确认…" : "确认选中"}</Button>
        {batchUndo && <Button size="sm" disabled={batchBusy} onClick={() => void undoBatch()}>{restoreBatch.isPending ? "正在撤销…" : "撤销批量确认"}</Button>}
      </div>
      <p>确认归属于 {name}。每批最多 {BATCH_LIMIT} 张人脸。{view === "photos" && "选择照片会选中其中与此联系人关联的候选人脸。"}</p>
      {batchNotice && <p role="status">{batchNotice}</p>}
      {batchError && <p role="alert">{batchError} {!!selectedIds.length && <Button size="sm" disabled={batchBusy} onClick={() => void confirmBatch()}>重试批量确认</Button>}</p>}
    </section>}
    {!batchMode && (batchNotice || batchUndo || batchError) && <div className={styles.batchFeedback}>
      {batchNotice && <p role="status">{batchNotice}</p>}
      {batchError && <p role="alert">{batchError}</p>}
      {batchUndo && <Button size="sm" disabled={batchBusy} onClick={() => void undoBatch()}>撤销批量确认</Button>}
    </div>}
    {changed && <Button size="sm" disabled={batchBusy} onClick={refresh}>照片有更新，点击刷新</Button>}
    {linkError && <p role="status">这张照片在当前资料下不可用，可从下方浏览其他照片。</p>}
    {photos.isPending && <Loading />}
    {photos.isError && !rows.length && <Failure onRetry={() => void photos.refetch()}>照片读取失败。</Failure>}
    {photos.isSuccess && !rows.length && !photos.hasNextPage && <EmptyState title={state === "all" ? "还没有相关照片" : `还没有${stateLabel[state]}照片`}>

    </EmptyState>}
    {view === "faces" ? <ul className={styles.faceGrid} aria-label="人脸缩略图">{rows.flatMap((photo) => photo.faces
      .filter((face) => face.profile_id === profileId && (state === "all" || face.status === state))
      .map((face) => <li key={face.id} className={styles.selectable} data-selected={selectedFaces.has(face.id)}>
        <button type="button" className={styles.faceCard} disabled={batchBusy} onClick={() => setSelected({ photo, faceId: face.id })}
          aria-label={`查看人脸 ${formatDateTime(photo.occurred_at, false)}，${stateLabel[face.status]}`}>
          <FaceThumbnail photo={photo} face={face} />
          <span className={styles.faceCaption}>
            <span className={styles.badge} data-status={face.status}>{stateLabel[face.status]}</span>
            <time dateTime={photo.occurred_at}>{formatDateTime(photo.occurred_at, false)}</time>
          </span>
        </button>
        {batchMode && <FaceSelection ids={face.status === "pending" ? [face.id] : []} selected={selectedFaces} busy={batchBusy}
          label={`选择人脸 ${formatDateTime(photo.occurred_at, false)}`} onToggle={toggleFaces} />}
      </li>))}</ul> : <ul className={styles.grid} aria-label="照片缩略图">{rows.map((photo) => {
      const own = photo.faces.filter((face) => face.profile_id === profileId);
      const candidates = own.filter((face) => state !== "confirmed" && face.status === "pending").map((face) => face.id);
      return <li key={photo.media_id} className={styles.selectable} data-selected={candidates.some((id) => selectedFaces.has(id))}>
        <button type="button" className={styles.card} disabled={batchBusy} onClick={() => setSelected({ photo })} aria-label={`查看照片 ${formatDateTime(photo.occurred_at, false)}`}>
          <PhotoImage photo={{ ...photo, faces: own }} />
          <span className={styles.caption}>
            <span>{photo.confirmed && <span className={styles.badge} data-status="confirmed">已确认</span>}
              {photo.pending && <span className={styles.badge} data-status="pending">候选</span>}</span>
            <span>{photo.sources[0] ? sourceLabel(photo.sources[0]) : "来源未记录"}</span>
            <time dateTime={photo.occurred_at}>{formatDateTime(photo.occurred_at, false)}</time>
          </span>
        </button>
        {batchMode && <FaceSelection ids={candidates} selected={selectedFaces} busy={batchBusy}
          label={`选择照片中的候选人脸 ${formatDateTime(photo.occurred_at, false)}`} onToggle={toggleFaces} />}
      </li>;
    })}</ul>}
    {photos.hasNextPage && <LoadMore cursor={photos.data?.pages.at(-1)?.next_cursor ?? ""} busy={photos.isFetchingNextPage || batchBusy}
      error={photos.isFetchNextPageError} onLoad={() => void loadMore()} />}
    {!!rows.length && !photos.hasNextPage && <p className={styles.end}>已显示全部照片</p>}
    {selected && <PhotoDetail photo={selected.photo} initialFaceId={selected.faceId} profileId={profileId} name={name} galleryKey={photoKey(profileId, state, sort)} onChange={clearBatch} onClose={() => setSelected(null)} />}
  </div>;
}

function PhotoDetail({ photo: initial, initialFaceId, profileId, name, galleryKey, onChange, onClose }: { photo: ProfilePhoto; initialFaceId?: string; profileId: string; name: string; galleryKey: readonly string[]; onChange: () => void; onClose: () => void }) {
  const [photo, setPhoto] = useState(initial);
  const [selected, setSelected] = useState(initialFaceId ?? initial.faces.find((f) => f.profile_id === profileId)?.id ?? initial.faces[0]?.id);
  const [picking, setPicking] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState<(() => void) | null>(null);
  const [undo, setUndo] = useState<{ states: FaceState[]; photo: ProfilePhoto; snapshots: [readonly unknown[], unknown][] } | null>(null);
  const decide = useDecideFaces();
  const restore = useRestoreFaces();
  const client = useQueryClient();
  const reading = usePhotoReading(photo.media_id);
  const face = photo.faces.find((f) => f.id === selected);
  const busy = decide.isPending || restore.isPending;

  async function act(decision: FaceDecision, targetName?: string) {
    setError(""); setRetry(null);
    const snapshots = client.getQueriesData({ queryKey: ["profile-photo-gallery"] });
    try {
      const result = await decide.mutateAsync(decision);
      onChange();
      setUndo({ states: result.previous, photo, snapshots });
      const nextFaces = photo.faces.flatMap((item): PhotoFace[] => item.id !== decision.face_ids[0] ? [item]
        : decision.action === "ignore" ? [] : [{ ...item,
          status: decision.action === "confirm" ? "confirmed" : "pending",
          profile_id: decision.action === "confirm" ? decision.profile_id : null,
          name: decision.action === "confirm" ? targetName ?? name : null,
        }]);
      const nextPhoto = { ...photo, faces: nextFaces };
      setPhoto(nextPhoto);
      for (const [key] of snapshots) {
        const pid = String(key[1]), filter = key[2] as PhotoState;
        if (JSON.stringify(key) !== JSON.stringify(galleryKey)) {
          for (const view of ["faces", "photos"]) positions.delete(JSON.stringify([...key, view]));
          client.removeQueries({ queryKey: key, exact: true }); continue;
        }
        client.setQueryData<InfiniteData<PhotoPage, string | null>>(key, (old) => old && ({ ...old, pages: old.pages.map((page) => ({ ...page,
          items: page.items.flatMap((item) => {
            if (item.media_id !== photo.media_id) return [item];
            const own = nextFaces.filter((f) => f.profile_id === pid);
            const pending = own.some((f) => f.status === "pending"), confirmed = own.some((f) => f.status === "confirmed");
            return own.length && (filter === "all" || (filter === "pending" ? pending : confirmed))
              ? [{ ...nextPhoto, pending, confirmed }] : [];
          }),
        })) }));
      }
      await client.invalidateQueries({ queryKey: ["profile-photo-status"] });
      setPicking(false); setNotice("已保存。后续匹配任务会读取本次反馈。");
    } catch {
      setError("保存失败，原归属已保留。"); setRetry(() => () => void act(decision, targetName));
    }
  }

  async function undoDecision() {
    if (!undo) return;
    setError(""); setRetry(null);
    try {
      await restore.mutateAsync(undo.states);
      onChange();
      for (const [key, data] of undo.snapshots) client.setQueryData(key, data);
      setPhoto(undo.photo); setUndo(null); setNotice("已撤销。");
      await client.invalidateQueries({ queryKey: ["profile-photo-status"] });
    } catch { setError("撤销失败，已保存的归属未改变。"); setRetry(() => () => void undoDecision()); }
  }

  return <Dialog title="照片详情" size="full" onClose={onClose} className={styles.dialog} locked={busy}>
    <div className={styles.detail}>
      <div className={styles.stage}><PhotoImage photo={photo} original selected={selected} onSelect={(id) => { setSelected(id); setPicking(false); }} />
        <a href={localMediaUrl(photo.media_id)} target="_blank" rel="noreferrer">打开原图</a>
      </div>
      <aside className={styles.info}>
        <h3>照片里的人</h3>
        <div className={styles.people}>{photo.faces.map((item) => <div key={item.id}>
          <button type="button" aria-pressed={item.id === selected} disabled={busy} onClick={() => { setSelected(item.id); setPicking(false); }}>
            {item.name || "未标注"} · {stateLabel[item.status]}
          </button>
          {item.profile_id && <Link href={`/profiles/${item.profile_id}?tab=photos&media=${photo.media_id}`} onClick={onClose}>资料</Link>}
        </div>)}</div>
        {face && <section className={styles.review} aria-label="选中人脸的归属">
          <strong>{face.name || "未标注"} · {stateLabel[face.status]}</strong>
          {face.score != null && <small>匹配分数 {face.score.toFixed(3)}</small>}
          <div className={styles.actions}>
            {face.profile_id && face.status === "pending" && <Button size="sm" disabled={busy} onClick={() => void act({ face_ids: [face.id], action: "confirm", profile_id: face.profile_id! }, face.name!)}>是本人</Button>}
            {face.profile_id && <Button size="sm" disabled={busy} onClick={() => void act({ face_ids: [face.id], action: "not", profile_id: face.profile_id! })}>不是本人</Button>}
            <Button size="sm" disabled={busy} onClick={() => setPicking(!picking)}>改为其他联系人</Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => void act({ face_ids: [face.id], action: "ignore" })}>忽略</Button>
          </div>
          {picking && <fieldset disabled={busy} className={styles.picker}><PersonPicker autoFocus onPick={(person) => void act({ face_ids: [face.id], action: "confirm", profile_id: person.id }, person.display_name)} /></fieldset>}
        </section>}
        {notice && <p role="status">{notice}</p>}
        {error && <p role="alert">{error} {retry && <Button size="sm" disabled={busy} onClick={retry}>重试</Button>}</p>}
        {undo && <Button size="sm" disabled={busy} onClick={() => void undoDecision()}>撤销上一步</Button>}
        <h3>照片来源</h3>
        {!photo.sources.length && <p>来源未记录，本地照片仍可查看。</p>}
        <ul className={styles.sources}>{photo.sources.map((source, index) => <li key={`${source.entity_type}-${source.entity_id}-${index}`}>
          <strong>{sourceLabel(source)}</strong>
          <span>{source.conversation_name || source.author || "发布者未记录"}</span>
          {source.occurred_at && <time>{formatDateTime(source.occurred_at)}</time>}
          {source.href ? <Link href={source.href}>查看{source.entity_type === "message_event" ? "原消息" : source.entity_type === "social_post" || source.entity_type === "social_story" ? "原动态" : "来源"}</Link>
            : <span>原始出处不可用</span>}
        </li>)}</ul>
        {reading.isPending && <p>正在读取图片描述…</p>}
        {reading.isError && <Failure onRetry={() => void reading.refetch()}>图片描述读取失败。</Failure>}
        {reading.data?.readings.filter((r) => photo.sources.some((s) => s.entity_id === r.entity_id)).map((r) => <details key={`${r.kind}-${r.entity_id}`}>
          <summary>图片识别</summary><p>{r.summary}</p>
          {r.items.filter((item) => !item.media_id || item.media_id === photo.media_id).map((item, i) => <div key={i}>
            {item.description && <p>{item.description}</p>}{item.text && <blockquote>{item.text}</blockquote>}
          </div>)}
        </details>)}
      </aside>
    </div>
  </Dialog>;
}
