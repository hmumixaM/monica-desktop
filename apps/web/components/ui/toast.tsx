"use client";

import { createContext, type ReactNode, useCallback, useContext, useMemo, useRef, useState } from "react";

import styles from "./overlay.module.css";

interface ToastOptions { message: ReactNode; actionLabel?: string; onAction?: () => void; duration?: number }
interface Toast extends ToastOptions { id: number; leaving?: boolean }

const ToastContext = createContext<(options: ToastOptions) => void>(() => undefined);

/** Brief confirmations, typically with an "撤销" action that is only offered while the toast is visible. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(0);
  const dismiss = useCallback((id: number) => {
    setToasts((list) => list.map((toast) => toast.id === id ? { ...toast, leaving: true } : toast));
    setTimeout(() => setToasts((list) => list.filter((toast) => toast.id !== id)), 140);
  }, []);
  const show = useCallback((options: ToastOptions) => {
    const id = ++next.current;
    setToasts((list) => [...list.slice(-2), { ...options, id }]);
    setTimeout(() => dismiss(id), options.duration ?? 5000);
  }, [dismiss]);
  return <ToastContext.Provider value={show}>
    {children}
    <div className={styles.toasts} role="status" aria-live="polite">
      {toasts.map((toast) => <ToastRow key={toast.id} toast={toast} onDismiss={() => dismiss(toast.id)} />)}
    </div>
  </ToastContext.Provider>;
}

function ToastRow({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const [used, setUsed] = useState(false);
  return <div className={styles.toast} data-leaving={toast.leaving ? "" : undefined}>
    <span>{toast.message}</span>
    {toast.actionLabel && toast.onAction && <button type="button" disabled={used} onClick={() => { setUsed(true); toast.onAction!(); onDismiss(); }}>
      {toast.actionLabel}
    </button>}
  </div>;
}

export function useToast() {
  const show = useContext(ToastContext);
  return useMemo(() => ({ show }), [show]);
}
