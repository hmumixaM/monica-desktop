"use client";

import { type ReactNode, useId, useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

import { Textarea } from "./field";
import styles from "./markdown.module.css";
import { Segmented } from "./segmented";

const PLUGINS = [remarkGfm, remarkBreaks];

// Images in a note become links, so opening a profile never fetches from another host.
const COMPONENTS: Components = {
  a: ({ node: _node, href = "", ...props }) => href.startsWith("/") || href.startsWith("#")
    ? <a href={href} {...props} />
    : <a href={href} target="_blank" rel="noreferrer" {...props} />,
  img: ({ src, alt }) => <a href={typeof src === "string" ? src : undefined} target="_blank" rel="noreferrer">{alt || "图片"}</a>,
};

/** Hand-written text as Markdown; a single line break stays a line break, and raw HTML is not rendered. */
export function Markdown({ children, className }: { children: string; className?: string }) {
  return <div className={[styles.markdown, className].filter(Boolean).join(" ")}>
    <ReactMarkdown remarkPlugins={PLUGINS} components={COMPONENTS}>{children}</ReactMarkdown>
  </div>;
}

/** Caps long content at about sixteen lines with a fade, and offers the rest only when something is hidden. */
export function Clamp({ children }: { children: ReactNode }) {
  const body = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [open, setOpen] = useState(false);
  useLayoutEffect(() => {
    const element = body.current!;
    const observer = new ResizeObserver(() => setOverflows(element.scrollHeight > element.clientHeight + 1));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return <div className={styles.clamp} data-overflows={overflows ? "" : undefined} data-open={open ? "" : undefined}>
    <div ref={body} className={styles.clampBody}>{children}</div>
    {(overflows || open) && <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "收起" : "展开全文"}</button>}
  </div>;
}

/** A textarea that grows with its text, with a preview of how the Markdown will read. */
export function MarkdownEditor({ label, value, onChange, placeholder, autoFocus }: {
  label: string; value: string; onChange: (value: string) => void; placeholder?: string; autoFocus?: boolean;
}) {
  const id = useId();
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  return <div className={styles.editor}>
    <div className={styles.editorBar}>
      <span id={id}>{label}</span>
      <Segmented size="sm" label={`${label}的编辑方式`} value={mode} onChange={setMode}
        options={[{ value: "edit", label: "编辑" }, { value: "preview", label: "预览" }]} />
    </div>
    {mode === "edit"
      ? <Textarea aria-labelledby={id} className={styles.textarea} rows={9} value={value} placeholder={placeholder} autoFocus={autoFocus}
        onChange={(event) => onChange(event.target.value)} />
      : <div className={styles.preview} aria-labelledby={id} role="region">
        {value.trim() ? <Markdown>{value}</Markdown> : <span className={styles.hint}>还没有内容。</span>}
      </div>}
    <small className={styles.hint}>支持 Markdown：<code>**粗体**</code> <code>- 列表</code> <code>- [ ] 待办</code> <code>## 小标题</code> <code>&gt; 引用</code> <code>[文字](链接)</code></small>
  </div>;
}
