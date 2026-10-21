(() => {
  let refreshing = false;
  const text = (id: string, value: string) => { document.getElementById(id)!.textContent = value; };
  const field = (id: string) => document.getElementById(id) as HTMLInputElement;
  void browser.storage.local.get(["localAIConfig", "localAIDevice"]).then(values => {
    const config = values.localAIConfig as LocalModel.Config | undefined;
    if (config) {
      field("endpoint").value = config.endpoint;
      field("model").value = config.model;
      field("mode").value = config.mode;
      field("api-key").placeholder = config.apiKey ? "已保存；同来源留空可保留" : "无需密钥可留空";
    }
    text("device-id", values.localAIDevice ? "发起设备：" + values.localAIDevice.id : "首次连接云端时登记设备");
  });
  document.getElementById("local-config")!.addEventListener("submit", event => {
    event.preventDefault();
    let permission: Promise<boolean>;
    try {permission = browser.permissions.request({origins: [LocalModel.permission(field("endpoint").value)]});}
    catch (error) {text("config-result", String(error)); return;}
    void permission.then(async granted => {
      if (!granted) {text("config-result", "来源权限未授权"); return;}
      const url = LocalModel.endpoint(field("endpoint").value);
      const config = {endpoint: url.href, apiKey: field("keyless").checked ? "" : field("api-key").value || undefined,
        model: field("model").value.trim(), mode: field("mode").value};
      text("config-result", "正在执行实际 JSON 模型请求…");
      const result = await browser.runtime.sendMessage({kind: "local-ai-validate", config});
      text("config-result", result.ok ? "验证通过，任务将在本设备执行" : result.message);
      if (result.ok) field("api-key").value = "";
    }).catch(error => text("config-result", String(error)));
  });
  document.getElementById("resume")!.addEventListener("click", () => {void browser.runtime.sendMessage({kind: "local-ai-resume"});});
  document.getElementById("pause")!.addEventListener("click", () => {void browser.runtime.sendMessage({kind: "local-ai-stop"});});
  async function update(kind: string) {
    if (refreshing) return;
    refreshing = true;
    try {
      const stored = await browser.storage.local.get("localAIState");
      text("executor-state", stored.localAIState?.message || "等待本机执行端");
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
