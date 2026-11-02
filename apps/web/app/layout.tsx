import type { Metadata, Viewport } from "next";
import { Noto_Serif_SC } from "next/font/google";

import "./styles/tokens.css";
import { PwaRegister } from "../components/pwa-register";
import { ToastProvider } from "../components/ui/toast";
import { QueryProvider } from "../lib/queries";
import { THEME_BOOTSTRAP } from "../lib/theme";

// Names and page titles are set like a printed card; body text stays on the system stack.
// Downloaded at build time and served from this origin, so no font request leaves the NAS.
const serif = Noto_Serif_SC({ weight: "600", variable: "--font-serif-sc", display: "swap", preload: false });

export const metadata: Metadata = {
  title: "Monica",
  description: "Private relationship dossier",
  manifest: "/manifest.webmanifest",
};

export const viewport: Viewport = {
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f3f4f6" },
    { media: "(prefers-color-scheme: dark)", color: "#171c26" },
  ],
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN" className={serif.variable} suppressHydrationWarning>
    <head><script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} /></head>
    <body><PwaRegister /><QueryProvider><ToastProvider>{children}</ToastProvider></QueryProvider></body>
  </html>;
}
