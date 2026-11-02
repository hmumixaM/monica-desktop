"use client";

import {Monitor, Moon, Sun} from "lucide-react";
import type {ReactNode} from "react";
import {setTheme, type ThemeChoice, useThemeChoice} from "../../lib/theme";
import {Section} from "../ui/primitives";
import {Segmented} from "../ui/segmented";
import styles from "./settings.module.css";

const OPTIONS: {value: ThemeChoice; label: ReactNode}[] = [
  {value: "system", label: <><Monitor size={14} />跟随系统</>},
  {value: "light", label: <><Sun size={14} />浅色</>},
  {value: "dark", label: <><Moon size={14} />深色</>},
];

export function Appearance() {
  const theme = useThemeChoice();
  return <Section title="外观" className={styles.panel}>
    <div className={styles.split}>
      <div className={styles.text}><strong>主题</strong><small>只保存在这台设备上。也可以在侧栏或 ⌘K 里切换。</small></div>
      <Segmented label="主题" value={theme} onChange={setTheme} options={OPTIONS} />
    </div>
  </Section>;
}
