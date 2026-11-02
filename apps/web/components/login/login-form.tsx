"use client";

import {ArrowRight, ShieldCheck} from "lucide-react";
import {useRouter} from "next/navigation";
import {type FormEvent, useState} from "react";
import {Button} from "../ui/button";
import {Field, Input} from "../ui/field";
import styles from "./login.module.css";

export function LoginForm() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [needsTotp, setNeedsTotp] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    await fetch("/api/v1/auth/csrf");
    const response = await fetch("/api/v1/auth/login", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({email, password, totp_code: totpCode || null, recovery_code: recoveryCode || null}),
    });
    if (response.ok) {
      router.replace("/dashboard");
      return;
    }
    const body = await response.json().catch(() => ({}));
    const totpRequired = response.status === 401 && body.detail === "TOTP code required";
    if (totpRequired) setNeedsTotp(true);
    setError(totpRequired ? "" : body.detail ?? "登录失败，请检查账号或密码。");
    setBusy(false);
  }

  return <form className={styles.form} onSubmit={(event) => void submit(event)}>
    <header>
      <h1>欢迎回来</h1>
      <p>你的关系档案，由自己掌握。</p>
    </header>
    <Field label="邮箱">
      <Input type="email" name="email" autoComplete="username" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" required autoFocus />
    </Field>
    <Field label="密码">
      <Input type="password" name="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="至少 12 个字符" required />
    </Field>
    {needsTotp && <fieldset className={styles.totp}>
      <legend><ShieldCheck size={14} />这个账号开启了两步验证</legend>
      <Field label="验证器代码">
        <Input inputMode="numeric" pattern="[0-9]{6}" maxLength={6} autoComplete="one-time-code" value={totpCode} onChange={(event) => setTotpCode(event.target.value)} placeholder="000000" autoFocus />
      </Field>
      <Field label="或使用恢复码">
        <Input value={recoveryCode} onChange={(event) => setRecoveryCode(event.target.value)} autoComplete="off" placeholder="一次性恢复码" />
      </Field>
    </fieldset>}
    {error && <p className={styles.error} role="alert">{error}</p>}
    <Button type="submit" variant="primary" size="lg" block disabled={busy}>{busy ? "验证中…" : needsTotp ? "验证并登录" : "登录"}<ArrowRight size={16} /></Button>
    <p className={styles.note}>登录会话和设备可以在设置中随时撤销。</p>
  </form>;
}
