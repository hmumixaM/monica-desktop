"use client";

import {Laptop, Smartphone} from "lucide-react";
import {useEffect, useState} from "react";
import {apiFetch} from "../../lib/api";
import {Button} from "../ui/button";
import {formatDateTime, Section} from "../ui/primitives";
import styles from "./settings.module.css";

type Device = {id: string; current: boolean; user_agent: string; revoked_at?: string; created_at: string};

export function DeviceSessions() {
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [error, setError] = useState("");

  async function load() {
    const response = await fetch("/api/v1/auth/sessions");
    if (response.ok) setDevices(await response.json());
  }
  useEffect(() => {void load();}, []);

  async function revoke(id: string) {
    const response = await apiFetch(`/api/v1/auth/sessions/${id}/revoke`, {method: "POST"});
    if (response.ok) await load();
    else setError("未能退出设备，请重试。");
  }

  const active = devices?.filter((device) => !device.revoked_at) ?? [];
  return <Section title="登录设备" className={styles.panel}>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {devices && <ul className={styles.list}>
      {active.map((device) => {
        const mobile = device.user_agent?.includes("Mobile");
        return <li key={device.id} className={styles.row}>
          <span className={styles.icon}>{mobile ? <Smartphone size={18} /> : <Laptop size={18} />}</span>
          <div className={styles.text}>
            <strong>{device.current ? "这台设备" : "其他设备"}</strong>
            <small>{mobile ? "移动浏览器" : "电脑浏览器"} · 登录于 {formatDateTime(device.created_at, false)}</small>
          </div>
          {!device.current && <Button size="sm" variant="ghost" aria-label="退出此设备" onClick={() => void revoke(device.id)}>退出</Button>}
        </li>;
      })}
    </ul>}
  </Section>;
}
