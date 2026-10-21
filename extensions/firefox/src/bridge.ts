(() => {
  if (window.top !== window || new URL(document.URL).origin !== Linkarium.ORIGIN) return;
  type Listener = Parameters<typeof browser.runtime.onMessage.addListener>[0];
  const scope = globalThis as typeof globalThis & {linkariumBridge?: {listener: Listener; page: (event: MessageEvent) => void; timer: ReturnType<typeof setInterval>}};
  if (scope.linkariumBridge) {
    browser.runtime.onMessage.removeListener(scope.linkariumBridge.listener);
    window.removeEventListener("message", scope.linkariumBridge.page);
    clearInterval(scope.linkariumBridge.timer);
  }
  const uuid = "[0-9a-f-]{36}";
  function allowed(path: unknown, method: unknown): path is string {
    if (typeof path !== "string") return false;
    return method === "POST" && ["/local-ai/devices", "/local-ai/claim", "/local-ai/heartbeat", "/local-ai/pause", "/local-ai/result", "/extension/submissions", "/bookmarks", "/classification/batches"].includes(path) ||
      method === "POST" && new RegExp("^/(?:extension/submissions/" + uuid + "/retry|jobs/" + uuid + "/retry|imports/" + uuid + "/execute)$").test(path) ||
      method === "GET" && (path === "/local-ai/device" || new RegExp("^/extension/submissions/" + uuid + "$").test(path));
  }
  async function request(message: any): Promise<LocalCloud.Reply> {
    const sessionResponse = await fetch("/api/v1/session", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(15000)});
    const session = await sessionResponse.json();
    if (!sessionResponse.ok) return {ok: false, code: "UNAVAILABLE", message: "Linkarium 暂时不可用"};
    if (!session.data?.authenticated) return {ok: false, code: "AUTH_REQUIRED", message: "请先登录 Linkarium 后恢复本机分类"};
    const headers: Record<string, string> = {"Content-Type": "application/json", "Idempotency-Key": message.key, "X-CSRFToken": session.data.csrf_token};
    if (message.device) {
      headers["X-Linkarium-Device"] = message.device.id;
      headers["X-Linkarium-Device-Proof"] = message.device.proof;
    }
    const response = await fetch("/api/v1" + message.path, {method: message.method, headers,
      body: message.body === undefined ? undefined : JSON.stringify(message.body), credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(20000)});
    const value = await response.json();
    if (response.ok && "data" in value) return {ok: true, data: value.data};
    return {ok: false, code: value.error?.code || "INVALID_RESPONSE", message: value.error?.message || "Linkarium 响应无效"};
  }
  const listener: Listener = (message, sender) => {
    if (sender.id !== browser.runtime.id || message?.kind !== "linkarium-cloud" || !allowed(message.path, message.method) || typeof message.key !== "string") return;
    return request(message).catch((): LocalCloud.Reply => ({ok: false, code: "NETWORK_ERROR", message: "云端连接中断，保留本机进度"}));
  };
  browser.runtime.onMessage.addListener(listener);
  const page = (event: MessageEvent) => {
    if (event.source !== window || event.origin !== Linkarium.ORIGIN || event.data?.channel !== "linkarium-page-v1" || typeof event.data.id !== "string") return;
    const message = event.data;
    if (!["linkarium-web", "linkarium-device-status", "linkarium-open-local-settings"].includes(message.kind)) return;
    void browser.runtime.sendMessage(message).then(result => window.postMessage({channel: "linkarium-reply-v1", id: message.id, result}, Linkarium.ORIGIN))
      .catch(() => window.postMessage({channel: "linkarium-reply-v1", id: message.id, result: {ok: false, code: "EXTENSION_UNAVAILABLE", message: "请重新加载插件"}}, Linkarium.ORIGIN));
  };
  window.addEventListener("message", page);
  let authenticated = false;
  const timer = setInterval(() => {
    if (authenticated) return;
    void fetch("/api/v1/session", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(10000)})
      .then(response => response.json()).then(session => {
        authenticated = Boolean(session.data?.authenticated);
        if (authenticated) return browser.runtime.sendMessage({kind: "linkarium-authenticated"});
      }).catch(() => {});
  }, 3000);
  scope.linkariumBridge = {listener, page, timer};
  void browser.runtime.sendMessage({kind: "linkarium-authenticated"}).catch(() => {});
})();
