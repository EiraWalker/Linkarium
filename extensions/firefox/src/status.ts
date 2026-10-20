(() => {
  let refreshing = false;
  const text = (id: string, value: string) => { document.getElementById(id)!.textContent = value; };
  async function update(kind: string) {
    if (refreshing) return;
    refreshing = true;
    try {
      const operation: Linkarium.Operation | undefined = await browser.runtime.sendMessage({kind});
      if (!operation) { text("state", "还没有通过插件收藏页面"); return; }
      text("state", Linkarium.label(operation));
      text("title", operation.title);
      text("url", operation.url);
      text("folder", operation.result?.folder_path ? "目录：" + operation.result.folder_path : "");
      text("error", operation.message || operation.result?.error_message || "");
      document.getElementById("retry")!.hidden = !(operation.state === "unconfirmed" || operation.state === "login_required" || operation.result?.can_retry);
      (document.getElementById("library") as HTMLAnchorElement).href = Linkarium.ORIGIN + "/";
    } catch { text("state", "暂时无法读取，请刷新重试"); }
    finally { refreshing = false; }
  }
  document.getElementById("refresh")!.addEventListener("click", () => { void update("linkarium-refresh"); });
  document.getElementById("retry")!.addEventListener("click", () => {
    void browser.permissions.request({origins: [Linkarium.SITE_PATTERN]}).then(granted => {
      if (granted) void update("linkarium-retry");
      else text("error", "请允许插件访问 Linkarium 后再重试");
    });
  });
  void update("linkarium-refresh");
  setInterval(() => { void update("linkarium-refresh"); }, 3000);
})();
