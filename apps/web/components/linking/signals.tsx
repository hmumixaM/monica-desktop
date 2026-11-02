import styles from "./linking.module.css";
import type {Signal} from "./types";

/** Identity values used for matching, such as remark, WeChat ID and group display names. */
export function Signals({signals}: {signals?: Signal[]}) {
  if (!signals?.length) return null;
  return <dl className={styles.signals}>
    {signals.map((signal) => <div key={signal.label}>
      <dt>{signal.label}</dt>
      <dd>{signal.values.join("、")}{signal.more > 0 && <span> 等 {signal.values.length + signal.more} 个</span>}</dd>
    </div>)}
  </dl>;
}
