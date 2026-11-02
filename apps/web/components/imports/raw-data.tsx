import styles from "./imports.module.css";

/** Source JSON, folded away by default: it is for auditing, not for reading. */
export function RawData({value, open, label = "原始数据"}: {value: unknown; open?: boolean; label?: string}) {
  return <details className={styles.raw} open={open}>
    <summary>{label}</summary>
    <pre>{JSON.stringify(value, null, 2)}</pre>
  </details>;
}
