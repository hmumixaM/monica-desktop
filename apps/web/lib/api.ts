"use client";

function readCookie(name: string) {
  return document.cookie
    .split("; ")
    .find((part) => part.startsWith(`${name}=`))
    ?.slice(name.length + 1);
}

async function csrfToken() {
  const existing = readCookie("crm_csrf");
  if (existing) return existing;
  const response = await fetch("/api/v1/auth/csrf", { credentials: "same-origin" });
  if (!response.ok) return "";
  const body = (await response.json()) as { csrf_token?: string };
  return body.csrf_token ?? readCookie("crm_csrf") ?? "";
}

export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = await csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
  }
  return fetch(input, { ...init, headers, credentials: "same-origin" });
}
