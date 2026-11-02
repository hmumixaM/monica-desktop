"use client";

import {Check, Copy, ShieldCheck} from "lucide-react";
import {type FormEvent, useEffect, useState} from "react";
import {apiFetch} from "../../lib/api";
import {Button} from "../ui/button";
import {Field, Input} from "../ui/field";
import {Section} from "../ui/primitives";
import {useToast} from "../ui/toast";
import styles from "./settings.module.css";

export function TotpSettings() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [secret, setSecret] = useState("");
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState<string[]>([]);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const toast = useToast();

  useEffect(() => {
    fetch("/api/v1/auth/totp/status").then((response) => response.json())
      .then((body) => setEnabled(body.enabled)).catch(() => setError("无法读取验证器设置"));
  }, []);

  async function setup() {
    const response = await apiFetch("/api/v1/auth/totp/setup", {method: "POST"});
    const body = await response.json();
    if (response.ok) setSecret(body.secret);
    else setError(body.detail);
  }

  async function verify(event: FormEvent) {
    event.preventDefault();
    const response = await apiFetch("/api/v1/auth/totp/verify", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({code}),
    });
    const body = await response.json();
    if (response.ok) {
      setEnabled(true);
      setSecret("");
      setError("");
      setRecovery(body.recovery_codes);
    } else setError("验证码无效，请重新输入。");
  }

  async function copy() {
    await navigator.clipboard.writeText(recovery.join("\n"));
    setCopied(true);
    toast.show({message: `已复制 ${recovery.length} 个恢复码`});
  }

  return <Section title="验证器登录" className={styles.panel}>
    <div className={styles.row}>
      <span className={styles.icon} data-on={enabled ? "" : undefined}><ShieldCheck size={18} /></span>
      <div className={styles.text}>
        <strong>{enabled ? "已启用两步验证" : "两步验证"}</strong>
        <small>{enabled ? "登录时需要验证器里的 6 位代码，或一次性恢复码。" : "为登录增加一次性验证码。验证成功后才启用。"}</small>
      </div>
      {enabled === false && !secret && <Button size="sm" onClick={() => void setup()}>设置验证器</Button>}
    </div>
    {secret && <form className={styles.setup} onSubmit={(event) => void verify(event)}>
      <p>在验证器中添加账号，填写此密钥：</p>
      <code className={styles.secret}>{secret}</code>
      <Field label="验证器代码">
        <Input value={code} onChange={(event) => setCode(event.target.value)} inputMode="numeric" autoComplete="one-time-code"
          pattern="[0-9]{6}" maxLength={6} placeholder="000000" required />
      </Field>
      <Button type="submit" variant="primary">验证并启用</Button>
    </form>}
    {recovery.length > 0 && <div className={styles.recovery}>
      <strong>保存恢复码</strong>
      <small>每个代码可用于登录一次，只在这里显示本次生成的代码。</small>
      <ul>{recovery.map((value) => <li key={value}><code>{value}</code></li>)}</ul>
      <div className={styles.actions}>
        <Button size="sm" onClick={() => void copy()}>{copied ? <Check size={14} /> : <Copy size={14} />}{copied ? "已复制" : "复制全部"}</Button>
        <Button size="sm" variant="ghost" onClick={() => setRecovery([])}>已保存，隐藏代码</Button>
      </div>
    </div>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
  </Section>;
}
