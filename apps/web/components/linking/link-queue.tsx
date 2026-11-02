"use client";

import Link from "next/link";
import {useInfiniteQuery} from "@tanstack/react-query";
import {fetchJson} from "../../lib/queries";
import {Search} from "lucide-react";
import {type FormEvent, useCallback, useDeferredValue, useEffect, useState} from "react";
import {Button, IconButton} from "../ui/button";
import {Dialog} from "../ui/dialog";
import {Input, SearchInput, Select} from "../ui/field";
import {Avatar, formatNumber, providerLabel} from "../ui/primitives";
import {Segmented} from "../ui/segmented";
import {LoadMore} from "../ui/load-more";
import {Signals} from "./signals";
import styles from "./linking.module.css";
import {BLOCKER_LABEL, type Candidate, type QueueRow, type Review, type StagedAction, type StagedItem} from "./types";

type ContactHit = {
  profile_id: string;
  display_name: string;
  avatar_media_id: string | null;
  accounts: {provider: string; display_name: string; username: string}[];
};

const STATUS = [
  {value: "unresolved", label: "待处理"},
  {value: "recommended", label: "高置信"},
  {value: "skipped", label: "已跳过"},
  {value: "no_match", label: "无匹配"},
  {value: "all", label: "全部"},
];
const PAGE_SIZE = 50;
const HIGH_SCORE = 70;

export function LinkQueue({version, staged, stage, unstage, locked, onCount, initialQuery, onQuery}: {
  version: number;
  staged: Record<string, StagedItem>;
  stage: (items: StagedItem[]) => void;
  unstage: (accountIds: string[]) => void;
  locked: boolean;
  onCount: (count: number) => void;
  initialQuery: string;
  onQuery: (query: string) => void;
}) {
  const [pair, setPair] = useState("");
  const [status, setStatus] = useState("unresolved");
  const [sort, setSort] = useState("confidence");
  const [text, setText] = useState(initialQuery);
  const query = useDeferredValue(text.trim());
  const [choice, setChoice] = useState<Record<string, string>>({});
  const [extras, setExtras] = useState<Record<string, Candidate[]>>({});
  const [searching, setSearching] = useState<QueueRow | null>(null);

  useEffect(() => {
    if (query === initialQuery.trim()) return;
    const timer = setTimeout(() => onQuery(query), 300);
    return () => clearTimeout(timer);
  }, [query, initialQuery, onQuery]);

  const review = useInfiniteQuery({
    queryKey: ["link-review", pair, status, sort, query, version],
    initialPageParam: 1,
    queryFn: ({pageParam}) => {
      const params = new URLSearchParams({status_filter: status, sort, page: String(pageParam), page_size: String(PAGE_SIZE)});
      if (pair) params.set("pair", pair);
      if (query) params.set("q", query);
      return fetchJson<Review>(`/api/v1/link-review?${params}`);
    },
    getNextPageParam: (last) => last.queue.length && last.page * last.page_size < last.total ? last.page + 1 : undefined,
  });
  const data = review.data?.pages[0];
  const error = review.isError ? "暂时无法读取候选匹配。" : "";
  useEffect(() => { if (data) onCount(data.unresolved); }, [data, onCount]);

  const candidates = useCallback((row: QueueRow) => {
    const extra = extras[row.account_id] || [];
    const seen = new Set(row.candidates.map((item) => item.profile_id));
    return [...extra.filter((item) => !seen.has(item.profile_id)), ...row.candidates];
  }, [extras]);

  const current = useCallback((row: QueueRow) => {
    const all = candidates(row);
    const id = staged[row.account_id]?.profile_id ?? choice[row.account_id];
    return all.find((item) => item.profile_id === id)
      ?? all.find((item) => item.status !== "rejected")
      ?? all[0] ?? null;
  }, [candidates, choice, staged]);

  function item(row: QueueRow, candidate: Candidate, action: StagedAction): StagedItem {
    return {
      account_id: row.account_id,
      action,
      profile_id: candidate.profile_id,
      expected_profile_id: row.profile_id,
      score: candidate.score,
      reasons: candidate.reasons,
      account_name: row.display_name,
      account_provider: row.provider,
      profile_name: candidate.display_name,
    };
  }

  function toggle(row: QueueRow, action: StagedAction) {
    const candidate = current(row);
    if (!candidate) return;
    if (staged[row.account_id]?.action === action) unstage([row.account_id]);
    else stage([item(row, candidate, action)]);
  }

  function choose(row: QueueRow, profileId: string) {
    setChoice((value) => ({...value, [row.account_id]: profileId}));
    const existing = staged[row.account_id];
    const candidate = candidates(row).find((entry) => entry.profile_id === profileId);
    if (existing && candidate) stage([item(row, candidate, existing.action)]);
  }

  const rows = [...new Map((review.data?.pages.flatMap((part) => part.queue) ?? []).map((row) => [row.account_id, row])).values()];
  const linkable = (row: QueueRow) => row.ready && !staged[row.account_id] && current(row);

  function stageMany(filter: (row: QueueRow) => boolean) {
    stage(rows.filter((row) => linkable(row) && filter(row)).map((row) => item(row, current(row)!, "confirm")));
  }

  function chooseContact(hit: ContactHit) {
    if (!searching) return;
    const row = searching;
    const candidate: Candidate = {
      profile_id: hit.profile_id,
      display_name: hit.display_name,
      providers: hit.accounts.map((account) => account.provider),
      username: hit.accounts.map((account) => account.username).join(" · "),
      score: 0,
      confidence: "weak",
      reasons: ["人工搜索选择"],
      pair: row.pair,
      pair_label: row.pair_label,
      ready: row.ready,
      status: "pending",
      expected_profile_id: row.profile_id,
    };
    setExtras((value) => ({...value, [row.account_id]: [candidate, ...(value[row.account_id] || [])]}));
    setChoice((value) => ({...value, [row.account_id]: hit.profile_id}));
    if (row.ready) stage([item(row, candidate, "confirm")]);
    setSearching(null);
  }

  const highCount = rows.filter((row) => linkable(row) && current(row)!.score >= HIGH_SCORE).length;
  const openCount = rows.filter(linkable).length;

  return <section className={styles.panel} aria-label="待关联">
    <div className={styles.toolbar}>
      <SearchInput wrapperClassName={styles.filter} value={text} onChange={(event) => {setText(event.target.value);}}
        placeholder="账号、用户名、备注、群内名或候选姓名" aria-label="搜索候选" />
      <Select value={pair} onChange={(event) => {setPair(event.target.value);}} aria-label="平台组合">
        <option value="">全部组合</option>
        {data?.pairs.map((entry) => <option key={entry.id} value={entry.id}>{entry.label}</option>)}
      </Select>
      <Segmented size="sm" label="状态" value={status} onChange={(value) => {setStatus(value);}} options={STATUS} />
      <Select value={sort} onChange={(event) => {setSort(event.target.value);}} aria-label="排序">
        <option value="confidence">置信度优先</option>
        <option value="username">用户名 A–Z</option>
      </Select>
    </div>
    <div className={styles.bulk}>
      <span className={styles.count}>{data ? `${formatNumber(data.total)} 个账号` : ""}</span>
      <Button size="sm" disabled={locked || !highCount} onClick={() => stageMany((row) => current(row)!.score >= HIGH_SCORE)}>勾选本页高置信（{highCount}）</Button>
      <Button size="sm" variant="ghost" disabled={locked || !openCount} onClick={() => stageMany(() => true)}>勾选本页全部（{openCount}）</Button>
    </div>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    <div className={styles.tableShell}>
      <table className={`${styles.table} ${styles.queue}`}>
        <caption className={styles.srOnly}>勾选后进入待提交列表，最后统一关联</caption>
        <thead><tr>
          <th scope="col"><span className={styles.srOnly}>勾选</span></th>
          <th scope="col">来源账号</th>
          <th scope="col">关联到</th>
          <th scope="col">分数</th>
          <th scope="col">依据</th>
          <th scope="col"><span className={styles.srOnly}>其他操作</span></th>
        </tr></thead>
        <tbody>
          {rows.map((row) => {
            const all = candidates(row);
            const candidate = current(row);
            const mark = staged[row.account_id];
            return <tr key={row.account_id} data-staged={mark?.action} data-conflict={row.conflict ? "" : undefined}>
              <td className={styles.checkCell}>
                <input type="checkbox" aria-label={`关联 ${row.display_name}`} checked={mark?.action === "confirm"}
                  disabled={locked || !candidate || !row.ready} onChange={() => toggle(row, "confirm")} />
              </td>
              <th scope="row" className={styles.accountCell}>
                <div className={styles.account}>
                  <Avatar name={row.display_name || row.username} mediaId={row.avatar_media_id} size={32} />
                  <span>
                    <strong>{row.display_name}</strong>
                    <small>{providerLabel(row.provider)} · {row.username}</small>
                    {row.profile_id && <Link href={`/profiles/${row.profile_id}`}>现属 {row.profile_name || "查看"}</Link>}
                    {row.blocked_reason && <em className={styles.blocked}>{BLOCKER_LABEL[row.blocked_reason] ?? row.blocked_reason}</em>}
                  </span>
                </div>
                <Signals signals={row.signals} />
              </th>
              <td className={styles.pickCell}>
                {all.length ? <div className={styles.pick}>
                  <Select value={candidate?.profile_id ?? ""} disabled={locked} aria-label={`为 ${row.display_name} 选择人物`}
                    onChange={(event) => choose(row, event.target.value)}>
                    {all.map((entry) => <option key={entry.profile_id} value={entry.profile_id}>
                      {entry.display_name} · {entry.providers.map(providerLabel).join(" / ")}
                    </option>)}
                  </Select>
                  <IconButton size="sm" outlined disabled={locked} label="搜索其他人" onClick={() => setSearching(row)}><Search size={14} /></IconButton>
                </div> : <Button size="sm" onClick={() => setSearching(row)}><Search size={14} />搜索人物</Button>}
                <Signals signals={candidate?.signals} />
                {row.conflict && <small className={styles.warn}>另有账号也指向此人</small>}
              </td>
              <td className={styles.scoreCell}><b className={styles.score} data-confidence={candidate?.confidence}>{candidate?.score ?? "—"}</b></td>
              <td className={styles.reasons}>
                {candidate?.face_score != null && <span data-face="" title="账号头像里的脸与这个人已知的脸相比">头像人脸相似 {Math.round(candidate.face_score * 100)}%</span>}
                {candidate?.reasons.map((reason) => <span key={reason}>{reason}</span>)}
              </td>
              <td className={styles.rowActions}>
                <button type="button" aria-pressed={mark?.action === "reject"} disabled={locked || !candidate} onClick={() => toggle(row, "reject")}>不是同一人</button>
                <button type="button" aria-pressed={mark?.action === "skip"} disabled={locked || !candidate} onClick={() => toggle(row, "skip")}>跳过</button>
              </td>
            </tr>;
          })}
        </tbody>
      </table>
      {!data && !error && <p className={styles.empty} role="status">正在计算候选匹配，第一次打开约需十几秒…</p>}
      {data && !rows.length && <p className={styles.empty}>当前筛选下没有候选。</p>}
      {review.hasNextPage && <LoadMore cursor={String(review.data?.pages.at(-1)?.page)} busy={review.isFetchingNextPage || locked}
        error={review.isFetchNextPageError} onLoad={() => void review.fetchNextPage()} />}
    </div>
    {searching && <ContactSearch row={searching} onClose={() => setSearching(null)} onChoose={chooseContact} />}
  </section>;
}

function ContactSearch({row, onClose, onChoose}: {row: QueueRow; onClose: () => void; onChoose: (hit: ContactHit) => void}) {
  const [text, setText] = useState(row.display_name);
  const [hits, setHits] = useState<ContactHit[] | null>(null);

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!text.trim()) return;
    const params = new URLSearchParams({q: text.trim(), provider: row.provider});
    const response = await fetch(`/api/v1/link-review/contacts?${params}`);
    setHits(response.ok ? await response.json() as ContactHit[] : []);
  }

  return <Dialog title="搜索人物" description={`${providerLabel(row.provider)} · ${row.display_name}`} onClose={onClose}>
    <form className={styles.searchForm} onSubmit={(event) => void search(event)}>
      <Input autoFocus value={text} onChange={(event) => setText(event.target.value)} placeholder="姓名、账号或备注" aria-label="搜索人物" />
      <Button variant="primary" type="submit"><Search size={14} />搜索</Button>
    </form>
    <ul className={styles.hits}>
      {hits?.map((hit) => <li key={hit.profile_id}>
        <Avatar name={hit.display_name} mediaId={hit.avatar_media_id} size={28} />
        <span><strong>{hit.display_name}</strong><small>{hit.accounts.map((account) => `${providerLabel(account.provider)} · ${account.username}`).join(" / ")}</small></span>
        <Button size="sm" onClick={() => onChoose(hit)}>选择</Button>
      </li>)}
      {hits && !hits.length && <li className={styles.empty}>没有找到还缺少{providerLabel(row.provider)}账号的人。</li>}
    </ul>
  </Dialog>;
}
