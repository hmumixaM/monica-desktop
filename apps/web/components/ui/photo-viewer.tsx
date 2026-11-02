"use client";

import { ExternalLink, X, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { isLocalMediaUrl } from "../../lib/media";
import { useZoomFrom } from "../../lib/zoom";
import { IconButton } from "./button";
import styles from "./media.module.css";

/** Full-screen original photo, grown out of the thumbnail it was opened from. Only NAS media is shown. */
export function PhotoViewer({ src, alt, onClose, origin }: { src: string; alt: string; onClose: () => void; origin?: HTMLElement | null }) {
  const local = isLocalMediaUrl(src);
  const dialog = useRef<HTMLDialogElement>(null);
  const [zoom, setZoom] = useState(false);
  const close = useZoomFrom(dialog, origin);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className={styles.viewer} onClose={onClose} aria-label={alt}
    onCancel={(event) => { event.preventDefault(); close(); }}
    onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
    <header>
      <strong>{alt}</strong>
      <IconButton label={zoom ? "适合窗口" : "原尺寸放大"} onClick={() => setZoom((value) => !value)}>{zoom ? <ZoomOut size={18} /> : <ZoomIn size={18} />}</IconButton>
      {local && <a className={styles.viewerLink} href={src} target="_blank" rel="noreferrer" aria-label="打开原图" title="打开原图"><ExternalLink size={18} /></a>}
      <IconButton label="关闭原图" onClick={close}><X size={18} /></IconButton>
    </header>
    <div className={styles.stage} data-zoomed={zoom ? "" : undefined} onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
      {local ? <img data-zoom-target="" src={src} alt={alt} onClick={() => setZoom((value) => !value)} />
        : <span className={styles.unavailable}>图片尚未缓存或暂不可用</span>}
    </div>
  </dialog>;
}
