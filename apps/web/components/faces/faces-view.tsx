"use client";

import { Check, EyeOff, ScanFace, UserRoundX, UserRoundPlus } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { type FaceDecision, type FaceGroup, useDecideFaces, useFaceGroups, useFaceSummary, useRestoreFaces } from "../../lib/faces";
import { Button } from "../ui/button";
import { LoadMore } from "../ui/load-more";
import { Dialog } from "../ui/dialog";
import { PersonPicker } from "../ui/person-picker";
import { PersonLink } from "../ui/person-link";
import { Avatar, EmptyState, Failure, formatDateTime, formatNumber, Page, PageHeader, Skeleton } from "../ui/primitives";
import { useToast } from "../ui/toast";
import { FaceCrop } from "./face-crop";
import styles from "./faces.module.css";

export function FacesView() {
  const summary = useFaceSummary();
  const groups = useFaceGroups();
  const items = groups.data?.pages.flatMap((page) => page.items) ?? [];
  const s = summary.data;

  return <Page>
    <PageHeader title="照片里的人" />
    {s && <p className={styles.stats}>
      <span><b>{formatNumber(s.to_review)}</b> 张待确认</span>
      <span><b>{formatNumber(s.confirmed)}</b> 张已确认，涉及 <b>{formatNumber(s.people)}</b> 人</span>
      <span>已看过 <b>{formatNumber(s.scanned)}</b> 张照片{s.last_scan_at ? ` · 最近一次 ${formatDateTime(s.last_scan_at)}` : ""}</span>
    </p>}
    {groups.isError && <Failure onRetry={() => void groups.refetch()}>暂时无法读取人脸。</Failure>}
    {groups.isPending && <div className={styles.groups}>{[0, 1, 2].map((n) => <Skeleton key={n} height={180} />)}</div>}
    {groups.isSuccess && !items.length && <EmptyState icon={<ScanFace size={28} aria-hidden="true" />}
      title={s?.scanned ? "都确认完了" : "还没有扫描过照片"}>
      {s?.scanned ? "新的照片会在夜里扫描，第二天出现在这里。" : "在家里的 Mac 上装好 monica-faces 并在导入中心创建人脸令牌后，每晚 1:30 开始扫描。"}
    </EmptyState>}
    <div className={styles.groups}>{items.map((group) => <Group key={group.key} group={group} />)}</div>
    {groups.hasNextPage && <LoadMore cursor={String(items.length)} busy={groups.isFetchingNextPage} error={groups.isFetchNextPageError}
      onLoad={() => void groups.fetchNextPage()} />}
  </Page>;
}

function Group({ group }: { group: FaceGroup }) {
  const decide = useDecideFaces();
  const restore = useRestoreFaces();
  const toast = useToast();
  const [left, setLeft] = useState<Set<string>>(new Set());
  const [picking, setPicking] = useState(false);
  const chosen = group.faces.filter((face) => !left.has(face.id)).map((face) => face.id);
  const person = group.person;

  function act(decision: FaceDecision, message: string) {
    decide.mutate(decision, {
      onSuccess: ({ previous }) => toast.show({ message, actionLabel: "撤销", onAction: () => restore.mutate(previous), duration: 8000 }),
      onError: () => toast.show({ message: "没能保存，请重试" }),
    });
  }

  const toggle = (id: string) => setLeft((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return <section className={styles.group} aria-label={person ? `可能是 ${person.name}` : "未认出的人"}>
    <header>
      {person
        ? <PersonLink href={`/profiles/${person.profile_id}`} className={styles.who}>
          <Avatar name={person.name} mediaId={person.avatar_media_id} size={36} round />
          <span><small>可能是</small><strong>{person.name}</strong></span>
        </PersonLink>
        : <span className={styles.who}><span className={styles.unknown}><ScanFace size={18} aria-hidden="true" /></span>
          <span><small>反复出现</small><strong>未认出的人</strong></span></span>}
      <span className={styles.count}>{formatNumber(group.size)} 张{group.size > group.faces.length ? ` · 先看 ${group.faces.length} 张` : ""}</span>
      <div className={styles.actions}>
        {person && <Button size="sm" variant="primary" disabled={!chosen.length || decide.isPending}
          onClick={() => act({ face_ids: chosen, action: "confirm", profile_id: person.profile_id }, `已确认 ${chosen.length} 张是 ${person.name}`)}>
          <Check size={14} />是 TA{left.size ? `（${chosen.length}）` : ""}
        </Button>}
        <Button size="sm" variant={person ? "secondary" : "primary"} disabled={!chosen.length || decide.isPending} onClick={() => setPicking(true)}>
          <UserRoundPlus size={14} />{person ? "是别人" : "这是…"}
        </Button>
        {person && <Button size="sm" variant="ghost" disabled={!chosen.length || decide.isPending}
          onClick={() => act({ face_ids: chosen, action: "not", profile_id: person.profile_id }, `已记下不是 ${person.name}`)}>
          <UserRoundX size={14} />不是 TA
        </Button>}
        <Button size="sm" variant="ghost" disabled={!chosen.length || decide.isPending}
          onClick={() => act({ face_ids: chosen, action: "ignore" }, `已忽略 ${chosen.length} 张`)}>
          <EyeOff size={14} />忽略
        </Button>
      </div>
    </header>
    <ul className={styles.faces}>
      {group.faces.map((face) => <li key={face.id}>
        <button type="button" className={styles.face} aria-pressed={!left.has(face.id)} onClick={() => toggle(face.id)}
          title={left.has(face.id) ? "点一下加回这次操作" : "点一下把这张留到以后"}>
          <FaceCrop mediaId={face.media_id} box={face.box} label={person ? `可能是 ${person.name} 的脸` : "未认出的脸"} />
          {face.score != null && <span className={styles.score}>{Math.round(face.score * 100)}</span>}
        </button>
        {face.post_id && <Link className={styles.post} href={`/timeline?event=${face.post_id}`}>动态</Link>}
      </li>)}
    </ul>
    {left.size > 0 && <p className={styles.hint}>灰掉的 {left.size} 张这次不处理，会留在这里。</p>}
    {picking && <Dialog title="这些是谁？" description={`把选中的 ${chosen.length} 张脸指给一个人。`} onClose={() => setPicking(false)}>
      <PersonPicker autoFocus onPick={(picked) => {
        setPicking(false);
        act({ face_ids: chosen, action: "confirm", profile_id: picked.id }, `已确认 ${chosen.length} 张是 ${picked.display_name}`);
      }} />
    </Dialog>}
  </section>;
}
