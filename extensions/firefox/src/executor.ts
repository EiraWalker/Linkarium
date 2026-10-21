namespace LocalExecutor {
  interface Claim {
    protocol: number; request_id: string; job_id: string; attempt_id: string; lease_token: string;
    prompt_digest: string; stage: string; messages: unknown[]; schema: object; timeout_seconds: number;
  }
  interface Work {claim: Claim; response?: string; phase: "claimed" | "calling" | "waiting_upload"}
  let active: Promise<void> | undefined;
  let nextWake = 0;
  let validation: Promise<{ok: boolean; message?: string}> | undefined;
  let retainedResult: Work | undefined;
  let manualResume = false;
  const WORK = "localAIWork";
  export async function state(message: string, phase: string) {
    await browser.storage.local.set({localAIState: {message, phase, at: Date.now()}});
  }
  async function persist(work: Work): Promise<boolean> {
    try {
      await browser.storage.local.set({[WORK]: work});
      retainedResult = undefined;
      nextWake = 0;
      return true;
    } catch {
      retainedResult = work;
      nextWake = Infinity;
      const claim = work.claim;
      await LocalCloud.request("/local-ai/pause", {request_id: claim.request_id, attempt_id: claim.attempt_id,
        lease_token: claim.lease_token, prompt_digest: claim.prompt_digest, error_code: "LOCAL_STORAGE_UNAVAILABLE"});
      await browser.storage.local.set({localAIPaused: true}).catch(() => {});
      await state(work.response ? "完整结果尚未写入本机缓存，已停止自动重试；请释放空间后恢复" : "本机缓存不可写，任务已暂停；请释放空间后恢复", "storage_blocked").catch(() => {});
      return false;
    }
  }
  async function pump() {
    const stored = await browser.storage.local.get(["localAIConfig", WORK, "localAIPaused"]);
    let config = stored.localAIConfig as LocalModel.Config | undefined;
    if (stored.localAIPaused) {await state("已暂停；由本设备点击恢复", "paused"); return;}
    let work = retainedResult || stored[WORK] as Work | undefined;
    if (retainedResult && !await persist(retainedResult)) return;
    if (!config?.verified && !work?.response) {await state("等待本设备配置并验证 API", "configuration_required"); return;}
    for (let count = 0; count < 6; count++) {
      const reply = await LocalCloud.request("/local-ai/claim", {...(work ? {resume_request_id: work.claim.request_id, has_response: Boolean(work.response)} : {}), manual_resume: manualResume});
      manualResume = false;
      if (!reply.ok) {
        await state(reply.message, reply.code);
        if (["NOT_FOUND", "RESULT_INVALIDATED", "ATTEMPT_EXPIRED"].includes(reply.code)) await browser.storage.local.remove(WORK);
        return;
      }
      if (!reply.data) {await state("等待发起于本设备的分类任务", "waiting_device"); return;}
      if (reply.data.waiting) {
        await state(reply.data.state === "waiting_materials" ? "等待云端采集分类材料" : "等待原规划任务生成共享分类体系", reply.data.state);
        setTimeout(() => {void wake();}, 2000);
        return;
      }
      if (reply.data.paused) {await state("本机缓存不可写导致暂停；释放空间后请在原设备恢复", "storage_blocked"); return;}
      if (reply.data.completed) {
        if (work?.response && await LocalModel.digest(work.response) !== reply.data.receipt.result_digest) {
          await state("云端确认与本机缓存不一致，请检查该任务；缓存已保留", "RESULT_CONFLICT");
          await browser.storage.local.set({localAIPaused: true});
          return;
        }
        await browser.storage.local.remove(WORK); work = undefined; continue;
      }
      if (reply.data.invalidated) {await browser.storage.local.remove(WORK); work = undefined; await state("任务已失效：" + reply.data.error_code, "invalidated"); return;}
      const claim = reply.data as Claim;
      if (claim.protocol !== 1) {await state("协议不兼容，请升级 Firefox 插件", "upgrade_required"); return;}
      if (work && work.claim.prompt_digest !== claim.prompt_digest) {await state("提示词已变化，缓存未回传；请处理失效任务", "invalidated"); return;}
      work = {...(work || {}), claim, phase: work?.response ? "waiting_upload" : "claimed"};
      if (!await persist(work)) return;
      const authorization = () => ({request_id: claim.request_id, attempt_id: claim.attempt_id,
        lease_token: claim.lease_token, prompt_digest: claim.prompt_digest});
      let leaseFailed = false;
      const timer = setInterval(() => {
        void LocalCloud.request("/local-ai/heartbeat", {...authorization(), state: work?.phase}).then(result => {if (!result.ok) leaseFailed = true;});
      }, 30000);
      try {
        if (!work.response) {
          config = (await browser.storage.local.get("localAIConfig")).localAIConfig as LocalModel.Config | undefined;
          if (!config?.verified) {await state("完整缓存已回传；等待验证本机 API", "configuration_required"); return;}
          work.phase = "calling";
          if (!await persist(work)) return;
          await state(claim.stage === "taxonomy" ? "本机正在规划共享分类体系" : "本机正在分类", "running");
          work.response = await LocalModel.complete({...config}, claim.messages, claim.schema, claim.timeout_seconds);
          work.phase = "waiting_upload";
          // No cloud write before the entire bounded model response is durable.
          if (!await persist(work)) return;
        }
        await state("已获得本机结果，等待云端确认", "waiting_upload");
        if (leaseFailed) {nextWake = Date.now() + 30000; return;}
        const accepted = await LocalCloud.request("/local-ai/result", {...authorization(), response: work.response});
        if (!accepted.ok) {
          await state(accepted.message + "（" + accepted.code + "）", accepted.code);
          if (["AI_INVALID_RESULT", "PROPOSAL_STALE", "SUBMISSION_STALE", "RESULT_CONFLICT", "RESULT_INVALIDATED", "NOT_FOUND"].includes(accepted.code)) {
            await LocalCloud.request("/local-ai/pause", {...authorization(), error_code: "LOCAL_API_INVALID"});
            await browser.storage.local.set({localAIPaused: true});
            // A rejected result is never retransmitted as a success. Resume creates a new attempt.
            await browser.storage.local.remove(WORK);
          }
          return;
        }
        await browser.storage.local.remove(WORK);
        await state(accepted.data.auto_applied ? "已自动归档" : claim.stage === "taxonomy" ? "共享分类体系已保存" : "已生成方案，等待网页校对", accepted.data.auto_applied ? "auto_applied" : "review_ready");
        work = undefined;
        if ((await browser.storage.local.get("localAIPaused")).localAIPaused) {await state("已暂停；由本设备点击恢复", "paused"); return;}
      } catch (error) {
        await LocalCloud.request("/local-ai/pause", {...authorization(), error_code: "LOCAL_API_UNAVAILABLE"});
        await state(error instanceof Error ? error.message : "本机执行中断，等待恢复", "paused");
        nextWake = Date.now() + 60000;
        return;
      } finally {clearInterval(timer);}
    }
  }
  export function wake(force = false) {
    if (active || !force && Date.now() < nextWake) return active;
    active = pump().finally(() => {active = undefined;}).catch(() => {nextWake = Date.now() + 60000;});
    return active;
  }
  browser.alarms.onAlarm.addListener(alarm => {if (alarm.name === "linkarium-local-ai") void wake();});
  void browser.alarms.create("linkarium-local-ai", {periodInMinutes: 1});
  browser.runtime.onMessage.addListener((message, sender) => {
    if (sender.id !== browser.runtime.id) return;
    if (sender.url === browser.runtime.getURL("status.html")) {
      if (message?.kind === "local-ai-validate") {
        if (validation) return Promise.resolve({ok: false, message: "正在验证配置，请等待当前验证结束"});
        validation = (async () => {
          const candidate = message.config as Omit<LocalModel.Config, "apiKey" | "verified"> & {apiKey?: string};
          const url = LocalModel.endpoint(candidate.endpoint);
          if (typeof candidate.model !== "string" || !candidate.model.trim() || candidate.model.length > 200 || !["json_object", "json_schema", "text"].includes(candidate.mode)) throw new Error("请输入模型并选择输出模式");
          const previous = (await browser.storage.local.get("localAIConfig")).localAIConfig as LocalModel.Config | undefined;
          if (candidate.apiKey === undefined && previous?.apiKey && new URL(previous.endpoint).origin !== url.origin) throw new Error("API 来源已改变，请重新输入密钥或明确选择不使用密钥");
          const apiKey = candidate.apiKey === undefined ? previous?.apiKey || "" : candidate.apiKey;
          if (typeof apiKey !== "string" || apiKey.length > 8192 || /[\r\n]/.test(apiKey)) throw new Error("API 密钥格式无效");
          const config: LocalModel.Config = {endpoint: url.href.replace(/\/$/, ""), apiKey, model: candidate.model.trim(), mode: candidate.mode, verified: false};
          await browser.storage.local.set({localAIConfig: config});
          const response = await LocalModel.complete(config, [{role: "user", content: 'Return exactly the JSON object {"ok":true}, without tools or commentary.'}],
            {type: "object", properties: {ok: {type: "boolean"}}, required: ["ok"], additionalProperties: false}, 120);
          if (JSON.stringify(JSON.parse(response)) !== '{"ok":true}') throw new Error("模型未按要求返回 JSON，请调整模型或输出模式");
          config.verified = true;
          await browser.storage.local.set({localAIConfig: config, localAIPaused: false});
          void wake(true);
          return {ok: true};
        })().catch(error => ({ok: false, message: error instanceof Error ? error.message : "验证失败"})).finally(() => {validation = undefined;});
        return validation;
      }
      if (message?.kind === "local-ai-resume") {manualResume = true; nextWake = 0; return browser.storage.local.set({localAIPaused: false}).then(() => wake(true));}
      if (message?.kind === "local-ai-stop") return browser.storage.local.set({localAIPaused: true}).then(() => state("当前请求结束后暂停", "paused"));
    }
    if (sender.frameId !== 0 || !sender.tab?.url || new URL(sender.tab.url).origin !== Linkarium.ORIGIN || sender.tab.incognito ||
        sender.tab.cookieStoreId && sender.tab.cookieStoreId !== "firefox-default") return;
    if (message?.kind === "linkarium-open-local-settings") return browser.runtime.openOptionsPage();
    if (message?.kind === "linkarium-device-status") return browser.storage.local.get(["localAIConfig", "localAIState"]).then(async values => ({
      protocol: 1, device_id: (await LocalCloud.identity()).id, verified: Boolean(values.localAIConfig?.verified), state: values.localAIState}));
    if (message?.kind === "linkarium-web" && LocalCloud.webRoute(message.path, message.method) &&
        typeof message.key === "string" && message.key.length <= 128 && JSON.stringify(message.body).length <= 200000) {
      return LocalCloud.request(message.path, message.body, message.key, "POST").then(async result => {
        if (result.ok) {
          if (/^\/jobs\/.+\/retry$/.test(message.path)) await browser.storage.local.set({localAIPaused: false});
          void wake(true);
        }
        return result;
      });
    }
  });
  void wake();
}
