namespace LocalModel {
  let tail: Promise<unknown> = Promise.resolve();
  export async function digest(raw: string): Promise<string> {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(raw));
    return Array.from(new Uint8Array(bytes), value => value.toString(16).padStart(2, "0")).join("");
  }
  export interface Config {endpoint: string; apiKey: string; model: string; mode: "json_object" | "json_schema" | "text"; verified: boolean}
  export function endpoint(value: string): URL {
    if (typeof value !== "string" || value.length > 2000) throw new Error("API 地址无效");
    const url = new URL(value.trim().replace(/\/+$/, ""));
    if (url.username || url.password || url.search || url.hash ||
        !(url.protocol === "https:" || url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))) {
      throw new Error("请使用 HTTPS 或本机回环 HTTP 地址，地址不能包含凭据、查询参数或片段");
    }
    return url;
  }
  export function permission(value: string): string {
    const url = endpoint(value);
    return url.protocol + "//" + url.hostname + "/*";
  }
  export function complete(config: Config, messages: unknown[], schema: object, seconds: number): Promise<string> {
    const request = tail.then(() => exchange(config, messages, schema, seconds));
    tail = request.then(() => undefined, () => undefined);
    return request;
  }
  async function exchange(config: Config, messages: unknown[], schema: object, seconds: number): Promise<string> {
    const url = endpoint(config.endpoint);
    if (!await browser.permissions.contains({origins: [permission(config.endpoint)]})) throw new Error("缺少本机 API 来源权限，请在设置中授权");
    const address = url.href.replace(/\/$/, "");
    const body = {model: config.model, messages, stream: false,
      ...(config.mode === "text" ? {} : {response_format: config.mode === "json_schema" ?
        {type: "json_schema", json_schema: {name: "linkarium_result", strict: true, schema}} : {type: "json_object"}})};
    const response = await fetch(address.endsWith("/chat/completions") ? address : address + "/chat/completions", {
      method: "POST", headers: {"Content-Type": "application/json", ...(config.apiKey ? {Authorization: "Bearer " + config.apiKey} : {})},
      body: JSON.stringify(body), credentials: "omit", redirect: "error", cache: "no-store", signal: AbortSignal.timeout(seconds * 1000)});
    if (!response.ok) throw new Error("本机 API 请求失败（HTTP " + response.status + "），请检查模型、密钥和输出模式");
    const reader = response.body?.getReader();
    if (!reader) throw new Error("模型响应为空");
    let bytes = 0;
    const chunks: Uint8Array[] = [];
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        bytes += chunk.value.byteLength;
        if (bytes > 512 * 1024) throw new Error("模型响应超过 512 KiB 限额");
        chunks.push(chunk.value);
      }
    } finally { await reader.cancel().catch(() => {}); }
    const buffer = new Uint8Array(bytes);
    let offset = 0;
    for (const chunk of chunks) {buffer.set(chunk, offset); offset += chunk.byteLength;}
    const envelope = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(buffer));
    const choice = envelope.choices?.[0];
    const content = choice?.message?.content;
    if (typeof content !== "string" || choice.message.tool_calls || choice.finish_reason !== "stop") throw new Error("模型必须返回完整非流式 JSON 文本，不能调用工具");
    JSON.parse(content);
    return content;
  }
}
