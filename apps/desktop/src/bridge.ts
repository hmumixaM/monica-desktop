import { invoke } from "@tauri-apps/api/core";
import type { DesktopStatus } from "./types";

export const getStatus = () => invoke<DesktopStatus>("get_status");
export async function command(
  command: string,
  payload: Record<string, unknown> = {},
) {
  const result = await invoke<{ accepted?: boolean }>("control_command", {
    command,
    payload,
  });
  if (!result?.accepted) return result;
  const deadline = Date.now() + 15 * 60_000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 750));
    const status = await getStatus();
    if (!status.operation) {
      if (status.error) throw new Error(status.error);
      return status;
    }
  }
  throw new Error("操作仍在进行。可以在运行概览中查看状态，暂时不要重复提交。");
}
export const openMonica = () => invoke<void>("open_monica");
export const openLogs = () => invoke<void>("open_logs");
export const chooseDirectory = () => invoke<string | null>("choose_directory");
export const chooseFile = () => invoke<string | null>("choose_file");
export const setAutostart = (enabled: boolean) =>
  invoke<void>("set_autostart", { enabled });
export const quitApp = () => invoke<void>("quit_app");
