"use client";

import {usePathname, useRouter, useSearchParams} from "next/navigation";
import {useCallback, useState} from "react";
import {Page, PageHeader} from "../ui/primitives";
import {Segmented} from "../ui/segmented";
import {ConfirmedLinks} from "./confirmed-links";
import {LinkQueue} from "./link-queue";
import styles from "./linking.module.css";
import {StagedTray} from "./staged-tray";
import {useStaged} from "./use-staged";

type Tab = "confirmed" | "queue";

/** `?tab=` picks the list; `?q=` searches the queue, which is how 关联资料 from an unlinked chat lands here. */
export function LinkingView() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const initialQuery = params.get("q") ?? "";
  const tab: Tab = params.get("tab") === "confirmed" || (params.get("tab") !== "queue" && !initialQuery) ? "confirmed" : "queue";
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [linkedCount, setLinkedCount] = useState<number | null>(null);
  const [queueCount, setQueueCount] = useState<number | null>(null);
  const {staged, stage, unstage, markFailed, clear} = useStaged();
  const refresh = useCallback(() => setVersion((value) => value + 1), []);
  const replace = useCallback((changes: Record<string, string | null>) => {
    const next = new URLSearchParams(window.location.search);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    const text = next.toString();
    router.replace(text ? `${pathname}?${text}` : pathname, {scroll: false});
  }, [pathname, router]);
  const onQuery = useCallback((q: string) => replace({q: q || null, tab: "queue"}), [replace]);

  return <Page width="wide" label="账号关联" className={styles.page}>
    <PageHeader title="账号关联"
      actions={<Segmented label="账号关联" value={tab} onChange={(value) => replace({tab: value})} options={[
        {value: "confirmed", label: "已关联", count: linkedCount ?? undefined},
        {value: "queue", label: "待关联", count: queueCount ?? undefined},
      ]} />} />
    <div hidden={tab !== "confirmed"}><ConfirmedLinks version={version} onCount={setLinkedCount} /></div>
    <div hidden={tab !== "queue"}>
      <LinkQueue version={version} staged={staged} stage={stage} unstage={unstage} locked={busy} onCount={setQueueCount}
        initialQuery={initialQuery} onQuery={onQuery} />
    </div>
    <StagedTray staged={staged} unstage={unstage} markFailed={markFailed} clear={clear} onBusy={setBusy} onApplied={refresh} />
  </Page>;
}
