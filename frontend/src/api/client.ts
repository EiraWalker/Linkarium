import type { Envelope, Session } from "./types";

let csrfToken = "";
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public fields?: Record<string, string[]>,
    public details?: unknown,
    public retryAfter?: string,
  ) {
    super(message);
  }
}
export function setCsrfToken(token: string) {
  csrfToken = token;
}
export const idempotencyKey = () => crypto.randomUUID();
export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : "操作未完成，请重试。";
export async function request<T>(
  path: string,
  options: {
    method?: string;
    body?: unknown;
    signal?: AbortSignal;
    key?: string;
  } = {},
): Promise<Envelope<T>> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET" && method !== "HEAD") headers["X-CSRFToken"] = csrfToken;
  if (options.key) headers["Idempotency-Key"] = options.key;
  const form = options.body instanceof FormData;
  if (options.body !== undefined && !form)
    headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      method,
      credentials: "same-origin",
      headers,
      signal: options.signal,
      body:
        options.body === undefined
          ? undefined
          : form
            ? (options.body as FormData)
            : JSON.stringify(options.body),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError(
      0,
      "NETWORK",
      "连接中断，已保留当前数据。写入结果可能尚未确认。",
    );
  }
  let payload: unknown;
  try {
    payload = response.status === 204 ? { data: null } : await response.json();
  } catch {
    throw new ApiError(
      response.status,
      "INVALID_RESPONSE",
      response.status >= 500
        ? "服务暂时不可达，稍后将重新连接。"
        : "服务器响应格式不正确。",
    );
  }
  if (!response.ok) {
    const error = (
      payload as {
        error?: {
          code?: string;
          message?: string;
          field_errors?: Record<string, string[]>;
          details?: unknown;
        };
      }
    ).error;
    if (response.status === 401)
      window.dispatchEvent(new Event("session-expired"));
    if (response.status === 503)
      window.dispatchEvent(new Event("service-maintenance"));
    throw new ApiError(
      response.status,
      error?.code ?? "REQUEST_FAILED",
      error?.message ?? "操作未完成。",
      error?.field_errors,
      error?.details,
      response.headers.get("Retry-After") ?? undefined,
    );
  }
  if (!payload || typeof payload !== "object" || !("data" in payload))
    throw new ApiError(0, "INVALID_RESPONSE", "服务器响应缺少 data。");
  return payload as Envelope<T>;
}
export async function getSession() {
  const { data } = await request<Session>("/session");
  if (
    typeof data.authenticated !== "boolean" ||
    typeof data.csrf_token !== "string"
  )
    throw new ApiError(0, "INVALID_SESSION", "无法验证登录状态。");
  setCsrfToken(data.csrf_token);
  return data;
}
export function queryString(
  values: Record<string, string | number | boolean | undefined | null>,
) {
  const p = new URLSearchParams();
  for (const [key, value] of Object.entries(values))
    if (value !== undefined && value !== null && value !== "")
      p.set(key, String(value));
  return p.toString();
}
export function safeUrl(url: string) {
  try {
    const parsed = new URL(url);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "#";
  } catch {
    return "#";
  }
}
