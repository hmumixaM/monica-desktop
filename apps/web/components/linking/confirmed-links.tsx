"use client";

import Link from "next/link";
import {useDeferredValue, useEffect, useState} from "react";
import {SearchInput} from "../ui/field";
import {Avatar, formatNumber, SourceMark} from "../ui/primitives";
import {Segmented} from "../ui/segmented";
import {LoadMore} from "../ui/load-more";
import {Signals} from "./signals";
import styles from "./linking.module.css";
import {type LinkedPerson, PROVIDERS} from "./types";

const COMBOS = [
  {value: "", label: "全部"},
  {value: "wechat,instagram", label: "微信 + Instagram"},
  {value: "wechat,linkedin", label: "微信 + LinkedIn"},
  {value: "instagram,linkedin", label: "Instagram + LinkedIn"},
  {value: "wechat,instagram,linkedin", label: "三个平台"},
];
const PAGE_SIZE = 50;

export function ConfirmedLinks({version, onCount}: {version: number; onCount: (count: number) => void}) {
  const [text, setText] = useState("");
  const query = useDeferredValue(text.trim());
  const [combo, setCombo] = useState("");
  const [people, setPeople] = useState<LinkedPerson[] | null>(null);
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);

  useEffect(() => {
    const params = new URLSearchParams();
    if (query) params.set("q", query);
    if (combo) params.set("providers", combo);
    let cancelled = false;
    void fetch(`/api/v1/link-review/linked?${params}`, {cache: "no-cache"}).then(async (response) => {
      if (!response.ok) throw new Error("linked");
      const body = await response.json() as LinkedPerson[];
      if (cancelled) return;
      setPeople(body);
      setError("");
      if (!query && !combo) onCount(body.length);
    }).catch(() => !cancelled && setError("暂时无法读取已关联的人。"));
    return () => {cancelled = true;};
  }, [query, combo, version, onCount]);

  return <section className={styles.panel} aria-label="已关联">
    <div className={styles.toolbar}>
      <SearchInput wrapperClassName={styles.filter} value={text} onChange={(event) => {setText(event.target.value); setPage(1);}}
        placeholder="姓名、昵称、用户名、备注或群内名" aria-label="搜索已关联的人" />
      <Segmented size="sm" label="平台组合" value={combo} onChange={(value) => {setCombo(value); setPage(1);}} options={COMBOS} />
      <span className={styles.count}>{people ? `${formatNumber(people.length)} 人` : ""}</span>
    </div>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    <div className={styles.tableShell}>
      <table className={`${styles.table} ${styles.linked}`}>
        <caption className={styles.srOnly}>同一人物下已经关联的跨平台账号</caption>
        <thead><tr><th scope="col">人物</th>{PROVIDERS.map((provider) => <th scope="col" key={provider}><SourceMark provider={provider} size={12} /></th>)}</tr></thead>
        <tbody>
          {people?.slice(0, page * PAGE_SIZE).map((person) => <tr key={person.profile_id}>
            <th scope="row">
              <Link className={styles.person} href={`/profiles/${person.profile_id}`}>
                <Avatar name={person.display_name} mediaId={person.avatar_media_id} size={32} />
                <strong>{person.display_name}</strong>
              </Link>
              <Signals signals={person.signals} />
            </th>
            {PROVIDERS.map((provider) => <td key={provider} data-empty={person.accounts.some((account) => account.provider === provider) ? undefined : ""}>
              {person.accounts.filter((account) => account.provider === provider).map((account) => <div className={styles.account} key={account.account_id}>
                <span className={styles.cardMark}><SourceMark provider={provider} size={12} /></span>
                <Avatar name={account.display_name || account.username} mediaId={account.avatar_media_id} size={24} />
                <span>
                  <strong>{account.display_name}</strong>
                  <small>{account.username}</small>
                  {account.link_label && <small className={styles.provenance} title={account.link_reasons?.join("；")}>
                    {account.link_label}{account.link_reasons?.length ? `：${account.link_reasons[0]}` : ""}
                  </small>}
                </span>
              </div>)}
            </td>)}
          </tr>)}
        </tbody>
      </table>
      {!people && !error && <p className={styles.empty} role="status">正在读取…</p>}
      {people && !people.length && <p className={styles.empty}>{query || combo ? "没有符合条件的人。" : "还没有跨平台关联的人。"}</p>}
      {people && page * PAGE_SIZE < people.length && <LoadMore cursor={String(page)} busy={false} onLoad={() => setPage((value) => value + 1)} />}
    </div>
  </section>;
}
