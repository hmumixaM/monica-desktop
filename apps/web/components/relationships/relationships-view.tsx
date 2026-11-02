"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { Page, PageHeader } from "../ui/primitives";
import { Segmented } from "../ui/segmented";
import { FootprintView } from "./footprint-view";
import { GraphView } from "./graph-view";
import { OrbitView } from "./orbit-view";
import styles from "./relationships.module.css";

type View = "orbit" | "graph" | "map";
const VIEWS: { value: View; label: string; description: string }[] = [
  { value: "orbit", label: "亲疏圈", description: "按最近的私聊和通话，把人排进 5 / 15 / 50 / 150 四层圈子。" },
  { value: "graph", label: "关系图", description: "朋友圈里的点赞、评论和提醒，以及共同的群。" },
  { value: "map", label: "足迹地图", description: "资料城市和带地点的动态，可以按月回放。" },
];

/** 关系与足迹: three views of the same network. Only the chosen one loads; the orbit is the lightest. */
export function RelationshipsView() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const focus = params.get("focus");
  const requested = params.get("view") as View | null;
  const view: View = VIEWS.some((option) => option.value === requested) ? requested! : focus ? "graph" : "orbit";
  const current = VIEWS.find((option) => option.value === view)!;
  const choose = (next: View) => {
    const search = new URLSearchParams(params.toString());
    if (next === "orbit") search.delete("view");
    else search.set("view", next);
    if (next !== "graph") search.delete("focus");
    const text = search.toString();
    router.replace(text ? `${pathname}?${text}` : pathname, { scroll: false });
  };

  return <Page width="full" label="关系与足迹">
    <PageHeader title="关系与足迹"
      actions={<Segmented label="视图" value={view} onChange={choose} options={VIEWS} className={styles.views} />} />
    {view === "orbit" && <OrbitView />}
    {view === "graph" && <GraphView focus={focus} />}
    {view === "map" && <FootprintView />}
  </Page>;
}
