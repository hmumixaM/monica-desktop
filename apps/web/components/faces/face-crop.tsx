"use client";

import { type CSSProperties, useState } from "react";

import type { Box } from "../../lib/faces";
import { localMediaUrl } from "../../lib/media";
import styles from "./faces.module.css";

const MARGIN = 1.5;

/** A square around one face, cut from the original photo in the browser. */
export function FaceCrop({ mediaId, box, size = 88, label }: { mediaId: string; box: Box; size?: number; label: string }) {
  const [natural, setNatural] = useState<{ width: number; height: number } | null>(null);
  let style: CSSProperties = { opacity: 0 };
  if (natural) {
    const [left, top, width, height] = box;
    const side = Math.max(width * natural.width, height * natural.height) * MARGIN;
    const scale = size / side;
    const cx = (left + width / 2) * natural.width, cy = (top + height / 2) * natural.height;
    style = {
      width: natural.width * scale, height: natural.height * scale,
      transform: `translate(${(side / 2 - cx) * scale}px, ${(side / 2 - cy) * scale}px)`,
    };
  }
  return <span className={styles.crop} style={{ "--crop": `${size}px` } as CSSProperties} role="img" aria-label={label}>
    <img src={localMediaUrl(mediaId)} alt="" loading="lazy" decoding="async" draggable={false} style={style}
      onLoad={(event) => setNatural({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} />
  </span>;
}
