import {
  startAuthentication, startRegistration,
  type PublicKeyCredentialCreationOptionsJSON, type PublicKeyCredentialRequestOptionsJSON,
  type RegistrationResponseJSON,
} from "@simplewebauthn/browser";
import { ApiError, errorMessage, getSession, request } from "../../api/client";
import type { PasskeyRegistrationResult } from "../../api/types";

export type EnrollmentIntent = { kind: "enroll" | "reset"; token: string } | { kind: "obsolete" };
export type RegistrationAuthorization =
  | { kind: "local" }
  | { kind: "bootstrap"; code: string }
  | { kind: "first_passkey"; code: string }
  | { kind: "enroll" | "reset"; token: string; code: string };
export interface PendingRegistration {
  challenge_id: string;
  credential: RegistrationResponseJSON;
}

const ENROLLMENT_STORAGE = "linkarium.passkey-enrollment";
type StoredEnrollment = { intent: Exclude<EnrollmentIntent, { kind: "obsolete" }>; expiresAt: number };

export function clearEnrollmentIntent() {
  try { sessionStorage.removeItem(ENROLLMENT_STORAGE); } catch { /* Storage can be disabled. */ }
}

function storedEnrollment(): StoredEnrollment | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(ENROLLMENT_STORAGE) ?? "null") as StoredEnrollment | null;
    if (saved && ["enroll", "reset"].includes(saved.intent?.kind) && typeof saved.intent.token === "string"
      && saved.intent.token && Number.isFinite(saved.expiresAt) && saved.expiresAt > Date.now()) return saved;
  } catch { /* Invalid or unavailable tab storage cannot authorize anything. */ }
  clearEnrollmentIntent();
  return null;
}

function expireTabEnrollment(deadline: number) {
  window.setTimeout(() => { storedEnrollment(); }, Math.max(0, deadline - Date.now()));
}

/** Keep an unfinished link in this tab across reloads; the server owns its real deadline. */
export function consumeEnrollmentIntent(): EnrollmentIntent | null {
  const params = new URLSearchParams(location.hash.slice(1));
  const kind = params.has("reset") ? "reset" : params.has("enroll") ? "enroll" : null;
  const intent: EnrollmentIntent | null = kind
    ? { kind, token: params.get(kind) ?? "" }
    : params.has("passkey-setup") ? { kind: "obsolete" } : null;
  const saved = storedEnrollment();
  if (!intent) {
    if (saved) expireTabEnrollment(saved.expiresAt);
    return saved?.intent ?? null;
  }
  let persisted = false;
  if (intent.kind === "obsolete") clearEnrollmentIntent();
  else {
    const expiresAt = saved?.intent.kind === intent.kind && saved.intent.token === intent.token
      ? saved.expiresAt : Date.now() + (intent.kind === "reset" ? 10 : 5) * 60_000;
    try {
      sessionStorage.setItem(ENROLLMENT_STORAGE, JSON.stringify({ intent, expiresAt }));
      expireTabEnrollment(expiresAt); persisted = true;
    }
    catch { /* Preserve the fragment if this browser cannot retain tab state. */ }
  }
  if (persisted || intent.kind === "obsolete") history.replaceState(history.state, "", location.pathname + location.search);
  return intent;
}

export function requirePasskeySupport() {
  if (!window.isSecureContext || !window.PublicKeyCredential || !navigator.credentials)
    throw new Error("请通过 HTTPS 或 localhost 使用支持 Passkey 的浏览器。");
}

export function passkeyError(error: unknown): string {
  if (error instanceof Error) {
    if (["NotAllowedError", "AbortError"].includes(error.name)) return "认证已取消或超时，请重试。";
    if (error.name === "InvalidStateError") return "此认证器已有这个网站的 Passkey，请使用它登录或选择其他认证器。";
    if (error.name === "NotSupportedError") return "当前浏览器或认证器不支持 Passkey，请更换后重试。";
    if (error.name === "ConstraintError") return "此认证器无法完成所需的身份验证，请选择其他认证器。";
    if (error.name === "SecurityError") return "当前网址无法使用 Passkey，请打开网站配置的 HTTPS 地址。";
  }
  if (error instanceof ApiError) {
    const messages: Record<string, string> = {
      AUTH_GRANT_INVALID: "链接或授权码无效，请核对后重试。",
      AUTH_GRANT_EXPIRED: "授权已过期，请重新获取链接和授权码。",
      AUTH_GRANT_LOCKED: "授权码错误次数过多，请重新获取授权。",
      AUTH_GRANT_USED: "此授权已使用，请用已创建的 Passkey 登录，或重新获取授权。",
      AUTH_ALREADY_INITIALIZED: "网站已完成初始化，请使用 Passkey 登录。",
      AUTH_LAST_CREDENTIAL: "这是最后一种可用登录凭据，添加另一种后才能删除。",
      AUTH_REVOKED: "登录凭据已撤销，请重新登录。",
      AUTH_ATTEMPT_IN_PROGRESS: "登记正在处理中，请稍后确认结果。",
    };
    if (messages[error.code]) return messages[error.code];
  }
  return errorMessage(error);
}

export function registrationMayHaveCompleted(error: unknown) {
  return error instanceof ApiError && (
    error.status === 0 || error.status >= 500 || error.code === "INVALID_RESPONSE" ||
    error.code === "AUTH_ATTEMPT_IN_PROGRESS"
  );
}

export async function createPasskey(name: string, authorization: RegistrationAuthorization): Promise<PendingRegistration> {
  requirePasskeySupport();
  const body = authorization.kind === "local" ? { name }
    : authorization.kind === "bootstrap" ? { name, bootstrap_code: authorization.code }
    : authorization.kind === "first_passkey" ? { name, first_passkey_code: authorization.code }
    : { name, enrollment_token: authorization.token, authorization_code: authorization.code };
  const { data } = await request<{
    options: PublicKeyCredentialCreationOptionsJSON; challenge_id: string; expires_at: string;
  }>("/session/passkey/register/options", { method: "POST", body });
  const credential = await startRegistration({ optionsJSON: data.options });
  return { challenge_id: data.challenge_id, credential };
}

export async function confirmPasskey(pending: PendingRegistration): Promise<PasskeyRegistrationResult> {
  const submit = async () => (await request<PasskeyRegistrationResult>(
    "/session/passkey/register/verify", { method: "POST", body: pending },
  )).data;
  try { return await submit(); }
  catch (error) {
    if (!registrationMayHaveCompleted(error)) throw error;
    // Refresh CSRF/session after a lost reply, then replay the same signed response.
    // An existing logged-in session alone does not prove this registration succeeded.
    await getSession();
    return await submit();
  }
}

export async function loginWithPasskey() {
  requirePasskeySupport();
  const { data } = await request<{ options: PublicKeyCredentialRequestOptionsJSON; challenge_id: string }>(
    "/session/passkey/login/options", { method: "POST", body: {} },
  );
  const credential = await startAuthentication({ optionsJSON: data.options });
  await request("/session/passkey/login/verify", {
    method: "POST", body: { credential, challenge_id: data.challenge_id },
  });
}
