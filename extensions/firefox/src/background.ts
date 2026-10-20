(() => {
  const LAST = "lastOperation";
  const busy = new Map<string, Promise<Linkarium.Operation>>();

  async function remember(operation: Linkarium.Operation) {
    await browser.storage.local.set({[Linkarium.PREFIX + operation.id]: operation});
    const title = Linkarium.label(operation) + " · Alt+Shift+L 查看进度";
    await browser.action.setTitle({title});
    const text = operation.state === "login_required" ? "登录" : operation.state === "unconfirmed" ? "?" :
      operation.result?.state === "classified" ? "✓" : operation.result?.state === "already_saved" ? "已有" :
      operation.result?.state === "needs_attention" ? "!" : "…";
    await browser.action.setBadgeText({text});
    await browser.action.setBadgeBackgroundColor({color: text === "!" || text === "?" ? "#8d4b1b" : "#315d65"});
    return operation;
  }

  async function bridgeTab(): Promise<browser.tabs.Tab> {
    const tabs = await browser.tabs.query({url: Linkarium.SITE_PATTERN, windowType: "normal"});
    const existing = tabs.find(tab => tab.url && new URL(tab.url).origin === Linkarium.ORIGIN &&
      !tab.incognito && (!tab.cookieStoreId || tab.cookieStoreId === "firefox-default"));
    return existing || browser.tabs.create({url: Linkarium.ORIGIN + "/", active: false});
  }

  async function send(tab: browser.tabs.Tab, request: Linkarium.BridgeRequest): Promise<Linkarium.Reply> {
    // A newly created site tab needs to finish loading its fixed content script.
    for (let attempt = 0; attempt < 30; attempt++) {
      try {
        const reply = await browser.tabs.sendMessage(tab.id!, request, {frameId: 0});
        if (reply && typeof reply.ok === "boolean") return reply;
      }
      catch { await new Promise(resolve => setTimeout(resolve, 250)); }
    }
    return {ok: false, code: "BRIDGE_UNAVAILABLE", message: "无法连接 Linkarium，请检查站点权限或稍后重试"};
  }

  async function perform(operation: Linkarium.Operation, action: "submit" | "status" | "retry") {
    if (busy.has(operation.id)) return busy.get(operation.id)!;
    const work = (async () => {
      try {
        if (!(await browser.permissions.contains({origins: [Linkarium.SITE_PATTERN]}))) {
          operation.state = "unconfirmed";
          operation.message = "请允许插件访问 Linkarium 后再重试";
          return remember(operation);
        }
        let tab = await bridgeTab();
        const request: Linkarium.BridgeRequest = {kind: "linkarium-api", action,
          key: action === "retry" ? operation.retry!.key : operation.id,
          url: operation.url, title: operation.title,
          submissionId: operation.result?.submission_id, revision: operation.retry?.revision};
        let reply = await send(tab, request);
        if (!reply.ok && reply.code === "BRIDGE_UNAVAILABLE") {
          // Repair only the fixed Linkarium bridge, never the source page.
          await browser.scripting.executeScript({target: {tabId: tab.id!, frameIds: [0]}, files: ["common.js", "bridge.js"]});
          reply = await send(tab, request);
        }
        if (!reply.ok && reply.code === "NETWORK_ERROR" && !operation.recoveryTried) {
          operation.recoveryTried = true;
          await remember(operation);
          reply = await send(tab, request);
        }
        if (reply.ok) {
          operation.state = "accepted";
          operation.result = reply.data;
          operation.message = undefined;
          operation.retry = undefined;
        } else if (reply.code === "AUTH_REQUIRED") {
          operation.state = "login_required";
          await browser.tabs.update(tab.id!, {active: true});
        } else {
          operation.state = operation.result ? "accepted" : "unconfirmed";
          operation.message = reply.message;
        }
      } catch {
        operation.state = operation.result ? "accepted" : "unconfirmed";
        operation.message = "连接中断，提交结果待确认";
      }
      return remember(operation);
    })();
    busy.set(operation.id, work);
    try { return await work; } finally { busy.delete(operation.id); }
  }

  async function latest(): Promise<Linkarium.Operation | undefined> {
    const stored = await browser.storage.local.get(LAST);
    if (typeof stored[LAST] !== "string") return;
    const operation: Linkarium.Operation | undefined = (await browser.storage.local.get(Linkarium.PREFIX + stored[LAST]))[Linkarium.PREFIX + stored[LAST]];
    if (operation?.state === "sending" && !busy.has(operation.id)) {
      operation.state = "unconfirmed";
      operation.message = "提交结果待确认，请使用原请求重试";
      await remember(operation);
    }
    return operation;
  }

  async function recoverRecent() {
    const stored = await browser.storage.local.get(null);
    for (const key of Object.keys(stored).filter(key => key.startsWith(Linkarium.PREFIX))) {
      const operation = stored[key] as Linkarium.Operation;
      if (!["sending", "unconfirmed"].includes(operation.state) || operation.restartRecoveryTried ||
          Date.now() - operation.createdAt > 120000 || busy.has(operation.id)) continue;
      operation.restartRecoveryTried = true;
      operation.state = "unconfirmed";
      await remember(operation);
      await perform(operation, operation.retry ? "retry" : operation.result ? "status" : "submit");
    }
  }

  browser.action.onClicked.addListener(async tab => {
    try {
      if (tab.incognito || (tab.cookieStoreId && tab.cookieStoreId !== "firefox-default")) throw new Error("首版支持普通 Firefox 标签页");
      const operation: Linkarium.Operation = {id: crypto.randomUUID(), url: Linkarium.pageUrl(tab.url || ""),
        title: Array.from(tab.title || tab.url || "").slice(0, 500).join(""), createdAt: Date.now(), state: "sending"};
      // Request while the toolbar gesture is still active; no browsing is collected in the background.
      const permission = browser.permissions.request({origins: [Linkarium.SITE_PATTERN]});
      await browser.storage.local.set({[LAST]: operation.id});
      await remember(operation);
      if (!(await permission)) {
        operation.state = "unconfirmed";
        operation.message = "请允许插件访问 Linkarium 后再重试";
        await remember(operation);
        return;
      }
      await perform(operation, "submit");
      const stored = await browser.storage.local.get(null);
      const old = Object.keys(stored).filter(key => key.startsWith(Linkarium.PREFIX))
        .sort((a, b) => stored[b].createdAt - stored[a].createdAt).slice(30);
      await browser.storage.local.remove(old);
    } catch (error) {
      await browser.action.setBadgeText({text: "!"});
      await browser.action.setTitle({title: error instanceof Error ? error.message : "无法收藏此页面"});
    }
  });

  browser.commands.onCommand.addListener(command => {
    if (command === "show-status") void browser.runtime.openOptionsPage();
  });

  browser.runtime.onMessage.addListener((message, sender) => {
    if (sender.id !== browser.runtime.id) return;
    if (message?.kind === "linkarium-authenticated" && sender.tab?.url?.startsWith(Linkarium.ORIGIN + "/")) {
      return browser.storage.local.get(null).then(async stored => {
        for (const key of Object.keys(stored).filter(key => key.startsWith(Linkarium.PREFIX))) {
          const operation = stored[key] as Linkarium.Operation;
          if (operation.state === "login_required" && Date.now() - operation.createdAt < 86400000) {
            await perform(operation, operation.retry ? "retry" : operation.result ? "status" : "submit");
          }
        }
      });
    }
    if (sender.url !== browser.runtime.getURL("status.html")) return;
    if (message?.kind === "linkarium-latest") return latest();
    if (message?.kind === "linkarium-refresh" || message?.kind === "linkarium-retry") {
      return latest().then(async operation => {
        if (!operation) return;
        if (message.kind === "linkarium-refresh" && !operation.result) return operation;
        if (message.kind === "linkarium-retry" && operation.result?.state === "needs_attention") {
          operation.retry ||= {key: crypto.randomUUID(), revision: operation.result.revision};
          await remember(operation);
        }
        return perform(operation, operation.retry ? "retry" : operation.result ? "status" : "submit");
      });
    }
  });
  // One recovery of a recent, explicitly clicked request; never an offline upload queue.
  void recoverRecent();
})();
