(() => {
  type Listener = Parameters<typeof browser.runtime.onMessage.addListener>[0];
  const scope = globalThis as typeof globalThis & {linkariumBridge?: {listener: Listener; timer: ReturnType<typeof setInterval>}};
  if (scope.linkariumBridge) {
    browser.runtime.onMessage.removeListener(scope.linkariumBridge.listener);
    clearInterval(scope.linkariumBridge.timer);
  }
  async function request(message: Linkarium.BridgeRequest, refresh = true): Promise<Linkarium.Reply> {
    if (new URL(document.URL).origin !== Linkarium.ORIGIN) {
      return {ok: false, code: "INVALID_ORIGIN", message: "Linkarium 页面来源不匹配"};
    }
    const sessionResponse = await fetch("/api/v1/session", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(15000)});
    const session = await sessionResponse.json();
    if (!sessionResponse.ok) return {ok: false, code: "UNAVAILABLE", message: "Linkarium 暂时不可用"};
    if (!session.data?.authenticated) {
      wasAuthenticated = false;
      return {ok: false, code: "AUTH_REQUIRED", message: "请先登录 Linkarium"};
    }
    const headers: Record<string, string> = {"Content-Type": "application/json", "Idempotency-Key": message.key, "X-CSRFToken": session.data.csrf_token};
    let path = "/api/v1/extension/submissions";
    let body: string | undefined;
    if (message.action === "submit") {
      body = JSON.stringify({url: Linkarium.pageUrl(message.url || ""), title: message.title || ""});
    } else {
      if (!/^[0-9a-f-]{36}$/.test(message.submissionId || "")) throw new Error("Invalid submission");
      path += "/" + message.submissionId;
      if (message.action === "retry") {
        path += "/retry";
        body = JSON.stringify({revision: message.revision});
      }
    }
    const response = await fetch(path, {method: body ? "POST" : "GET", headers,
      body, credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(15000)});
    if (response.status === 403 && refresh) return request(message, false);
    const value = await response.json();
    if (response.ok && typeof value.data?.submission_id === "string") return {ok: true, data: value.data};
    return {ok: false, code: value.error?.code || "INVALID_RESPONSE", message: value.error?.message || "Linkarium 响应无效"};
  }

  const listener: Listener = (message: Linkarium.BridgeRequest, sender) => {
    if (sender.id !== browser.runtime.id || message?.kind !== "linkarium-api" ||
        !["submit", "status", "retry"].includes(message.action) || typeof message.key !== "string") return;
    return request(message).catch((): Linkarium.Reply => ({ok: false, code: "NETWORK_ERROR", message: "连接中断，提交结果待确认"}));
  };
  browser.runtime.onMessage.addListener(listener);

  // A login may complete in the existing SPA or after a full navigation.
  let wasAuthenticated = false;
  async function announce(initial = false) {
    try {
      if (new URL(document.URL).origin !== Linkarium.ORIGIN) return;
      if (!initial) {
        const stored = await browser.storage.local.get(null);
        if (!Object.keys(stored).some(key => key.startsWith(Linkarium.PREFIX) && stored[key].state === "login_required")) return;
      }
      const response = await fetch("/api/v1/session", {credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(10000)});
      const session = await response.json();
      if (session.data?.authenticated && !wasAuthenticated) {
        await browser.runtime.sendMessage({kind: "linkarium-authenticated"});
      }
      wasAuthenticated = Boolean(session.data?.authenticated);
    } catch { /* A later login poll can recover without submitting anything. */ }
  }
  void announce(true);
  const timer = setInterval(() => { void announce(); }, 3000);
  scope.linkariumBridge = {listener, timer};
})();
