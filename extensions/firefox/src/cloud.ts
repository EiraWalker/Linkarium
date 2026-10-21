namespace LocalCloud {
  export type Reply = {ok: true; data: any} | {ok: false; code: string; message: string};
  export interface Device {id: string; proof: string; name: string}
  let creating: Promise<Device> | undefined;
  export function identity(): Promise<Device> {
    return creating ||= (async () => {
      const stored = (await browser.storage.local.get("localAIDevice")).localAIDevice as Device | undefined;
      if (stored) return stored;
      const device = {id: crypto.randomUUID(), proof: crypto.randomUUID() + crypto.randomUUID(), name: "Firefox"};
      await browser.storage.local.set({localAIDevice: device});
      return device;
    })();
  }
  async function tab(): Promise<browser.tabs.Tab> {
    const tabs = await browser.tabs.query({url: Linkarium.SITE_PATTERN, windowType: "normal"});
    return tabs.find(item => item.url && new URL(item.url).origin === Linkarium.ORIGIN && !item.incognito &&
      (!item.cookieStoreId || item.cookieStoreId === "firefox-default")) || browser.tabs.create({url: Linkarium.ORIGIN + "/", active: false});
  }
  async function send(target: browser.tabs.Tab, message: object): Promise<Reply> {
    for (let count = 0; count < 30; count++) {
      try {
        const reply = await browser.tabs.sendMessage(target.id!, message, {frameId: 0});
        if (reply && typeof reply.ok === "boolean") return reply;
      } catch {
        if (count === 10) await browser.scripting.executeScript({target: {tabId: target.id!, frameIds: [0]}, files: ["common.js", "bridge.js"]}).catch(() => {});
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    return {ok: false, code: "BRIDGE_UNAVAILABLE", message: "无法连接 Linkarium，请检查站点权限"};
  }
  export async function request(path: string, body?: unknown, key: string = crypto.randomUUID(), method = body === undefined ? "GET" : "POST"): Promise<Reply> {
    try {
      const target = await tab();
      const device = await identity();
      const registered = await send(target, {kind: "linkarium-cloud", path: "/local-ai/devices", method: "POST", body: device, key});
      if (!registered.ok) return registered.code === "NOT_FOUND" ?
        {ok: false, code: "UPGRADE_REQUIRED", message: "Linkarium 服务器尚不支持本机 AI，请先协调升级服务器与 Firefox 插件"} : registered;
      return await send(target, {kind: "linkarium-cloud", path, method, body, key, device});
    } catch {
      return {ok: false, code: "NETWORK_ERROR", message: "云端连接中断，等待原设备恢复"};
    }
  }
  export function webRoute(path: unknown, method: unknown): path is string {
    return method === "POST" && typeof path === "string" && (
      path === "/bookmarks" || path === "/classification/batches" ||
      /^\/imports\/[0-9a-f-]{36}\/execute$/.test(path) || /^\/jobs\/[0-9a-f-]{36}\/retry$/.test(path));
  }
}
