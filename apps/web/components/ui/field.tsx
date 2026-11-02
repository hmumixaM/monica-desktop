import { Search } from "lucide-react";
import { forwardRef, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";

import styles from "./controls.module.css";

const join = (...names: (string | false | undefined)[]) => names.filter(Boolean).join(" ");

/** A labelled control; the label wraps the control so no id pairing is needed. */
export function Field({ label, hint, children, className }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return <label className={join(styles.field, className)}><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>;
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...props }, ref) {
  return <input ref={ref} className={join(styles.input, className)} {...props} />;
});

export function Select({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={join(styles.input, className)} {...props} />;
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={join(styles.input, className)} {...props} />;
}

/** A search box with a leading glass and an optional trailing hint such as a shortcut key. */
export const SearchInput = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { trailing?: ReactNode; wrapperClassName?: string }>(
  function SearchInput({ className, trailing, wrapperClassName, ...props }, ref) {
    return <span className={join(styles.search, wrapperClassName)}>
      <Search size={15} aria-hidden="true" />
      <input ref={ref} type="search" className={join(styles.input, className)} {...props} />
      {trailing}
    </span>;
  });

export function Checkbox({ children, className, ...props }: InputHTMLAttributes<HTMLInputElement> & { children: ReactNode }) {
  return <label className={join(styles.check, className)}><input type="checkbox" {...props} />{children}</label>;
}
