import { useRef, useState } from "preact/hooks";
import { ApiError, getSession } from "../../api/client";
import type { PasskeyRegistrationResult } from "../../api/types";
import { Button, ErrorNotice, Field } from "../../components/UI";
import {
  clearEnrollmentIntent, confirmPasskey, createPasskey, passkeyError, registrationMayHaveCompleted,
  type EnrollmentIntent, type PendingRegistration, type RegistrationAuthorization,
} from "./passkeyClient";
import s from "./PasskeyEnrollment.module.css";

type Mode = "local" | Exclude<EnrollmentIntent, { kind: "obsolete" }>;

export function PasskeyEnrollment({ mode, onComplete, onLogin, disabled = false, initialization = false }: {
  mode: Mode;
  onComplete: (result: PasskeyRegistrationResult) => Promise<void>;
  onLogin?: () => void;
  disabled?: boolean;
  initialization?: boolean;
}) {
  const [name, setName] = useState("这台设备");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<PendingRegistration | null>(null);
  const [completed, setCompleted] = useState<PasskeyRegistrationResult | null>(null);
  const inFlight = useRef(false);
  const [registered, setRegistered] = useState(false);
  const kind = typeof mode === "string" ? mode : mode.kind;
  const codeLabel = initialization ? "初始化码" : "授权码";

  async function submit(event: Event) {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError("");
    let current = pending;
    try {
      await getSession();
      if (completed) {
        await onComplete(completed);
        return;
      }
      if (!current) {
        const authorization: RegistrationAuthorization = mode === "local" ? { kind: "local" }
          : { ...mode, code: code.trim() };
        current = await createPasskey(name.trim(), authorization);
        setPending(current);
      }
      const result = await confirmPasskey(current);
      if (kind !== "local") clearEnrollmentIntent();
      setCompleted(result);
      setPending(null); setCode("");
      if (kind !== "local" && !result.authenticated) {
        setRegistered(true);
        return;
      }
      await onComplete(result);
    } catch (failure) {
      if (failure instanceof ApiError && ["AUTH_GRANT_EXPIRED", "AUTH_GRANT_USED", "AUTH_GRANT_LOCKED", "AUTH_REVOKED"].includes(failure.code)) clearEnrollmentIntent();
      if (!current || !registrationMayHaveCompleted(failure)) setPending(null);
      setError(failure instanceof ApiError && initialization && failure.code.startsWith("AUTH_GRANT_")
        ? failure.code === "AUTH_GRANT_INVALID" ? "初始化码无效，请核对后重试。" : "初始化码已失效，请从服务器终端重新获取。"
        : failure instanceof ApiError && kind === "reset" && ["AUTH_GRANT_EXPIRED", "AUTH_GRANT_LOCKED"].includes(failure.code)
          ? "授权码已失效，请从服务器终端重新获取。" : passkeyError(failure));
    } finally {
      inFlight.current = false; setBusy(false);
    }
  }

  if (registered) return <div class={s.form}>
    <p role="status">Passkey 已登记，请使用刚创建的 Passkey 登录。</p>
    {onLogin && <Button primary onClick={onLogin}>前往登录</Button>}
  </div>;

  return <form onSubmit={submit} class={s.form} aria-busy={busy}>
    <ErrorNotice message={error} />
    {kind !== "local" && <Field label={codeLabel}>
      <input type="text" inputMode="numeric" autoComplete="one-time-code" autoCapitalize="none" spellcheck={false}
        required maxLength={128} value={code} disabled={busy || !!pending || !!completed || disabled}
        onInput={e => setCode(e.currentTarget.value)} placeholder={initialization ? "输入服务器终端显示的初始化码" : "输入另行提供的授权码"} />
    </Field>}
    <Field label="Passkey 名称">
      <input type="text" required maxLength={100} value={name} disabled={busy || !!pending || !!completed || disabled}
        autoComplete="off" onInput={e => setName(e.currentTarget.value)} placeholder="例如：办公电脑" />
    </Field>
    <Button primary type="submit" disabled={busy || disabled || (!pending && !completed && (!name.trim() || (kind !== "local" && !code.trim())))}>
      {busy ? "正在登记…" : completed ? "继续" : pending ? "确认登记结果" : kind === "local" ? "在本机添加 Passkey" : "创建 Passkey"}
    </Button>
  </form>;
}
