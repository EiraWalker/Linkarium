import type { Envelope } from "./types";

export interface ExecutorStatus {
  protocol: number;
  device_id: string;
  verified: boolean;
  state?: {message: string; phase: string};
}

export function extensionCall<T>(kind: string, fields: Record<string, unknown> = {}, timeout = 1500): Promise<T> {
  return new Promise((resolve, reject) => {
    const id = crypto.randomUUID();
    const listener = (event: MessageEvent) => {
      if (event.source !== window || event.origin !== location.origin || event.data?.channel !== "linkarium-reply-v1" || event.data.id !== id) return;
      clearTimeout(timer);
      window.removeEventListener("message", listener);
      if (event.data.result === undefined) reject(new Error("请安装或升级 Firefox 本机 AI 插件"));
      else resolve(event.data.result as T);
    };
    const timer = setTimeout(() => {
      window.removeEventListener("message", listener);
      reject(new Error("请在 Firefox 安装或升级 Linkarium 插件，并允许访问此站点"));
    }, timeout);
    window.addEventListener("message", listener);
    window.postMessage({channel: "linkarium-page-v1", id, kind, ...fields}, location.origin);
  });
}

export const executorStatus = () => extensionCall<ExecutorStatus>("linkarium-device-status");
export const openExecutorSettings = () => extensionCall("linkarium-open-local-settings");
export function needsExecutor(path: string, method: string): boolean {
  return method === "POST" && (path === "/bookmarks" || path === "/classification/batches" ||
    /^\/imports\/[0-9a-f-]{36}\/execute$/.test(path) || /^\/jobs\/[0-9a-f-]{36}\/retry$/.test(path));
}
export async function executeBound<T>(path: string, body: unknown, key?: string): Promise<Envelope<T>> {
  const result = await extensionCall<{ok: boolean; data?: T; code?: string; message?: string}>("linkarium-web",
    {path, method: "POST", body, key: key || crypto.randomUUID()}, 30000);
  if (!result.ok) throw Object.assign(new Error(result.message || "本机执行端暂不可用"), {code: result.code});
  return {data: result.data as T};
}
