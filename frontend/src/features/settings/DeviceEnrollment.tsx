import { useEffect, useRef, useState } from "preact/hooks";
import QRCode from "qrcode";
import { request } from "../../api/client";
import type { EnrollmentGrant } from "../../api/types";
import { Button, ErrorNotice, Field } from "../../components/UI";
import { passkeyError } from "../auth/passkeyClient";
import s from "./Passkeys.module.css";

export function DeviceEnrollment() {
  const [grant, setGrant] = useState<EnrollmentGrant | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const [now, setNow] = useState(Date.now);
  const canvas = useRef<HTMLCanvasElement>(null);
  const linkInput = useRef<HTMLInputElement>(null);
  const codeInput = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  const remaining = grant ? Math.max(0, Math.ceil((Date.parse(grant.expires_at) - now) / 1000)) : 0;
  const active = !!grant && remaining > 0;

  useEffect(() => {
    if (!grant) return;
    const tick = () => setNow(Date.now());
    const timer = setInterval(tick, 1000);
    document.addEventListener("visibilitychange", tick);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", tick); };
  }, [grant]);
  useEffect(() => {
    if (!active || !canvas.current || !grant) return;
    let current = true;
    void QRCode.toCanvas(canvas.current, grant.enrollment_url, { width: 220, margin: 2, errorCorrectionLevel: "M" })
      .catch(() => { if (current) setError("二维码生成失败，请复制链接添加设备。"); });
    return () => { current = false; };
  }, [grant, active]);

  async function generate() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError(""); setCopyStatus("");
    try {
      const { data } = await request<EnrollmentGrant>("/session/passkey/grants", { method: "POST", body: {} });
      setGrant(data); setNow(Date.now());
    } catch (failure) { setError(passkeyError(failure)); }
    finally { inFlight.current = false; setBusy(false); }
  }
  async function copy(value: string, label: string, input: HTMLInputElement | null) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyStatus(`${label}已复制。`);
    } catch {
      input?.focus(); input?.select();
      setCopyStatus(`请复制已选中的${label}。`);
    }
  }

  return <div class={s.deviceEnrollment}>
    <h3>添加其他设备</h3>
    <p class="muted">新手机扫码，或在新电脑打开链接，再输入授权码。</p>
    <ErrorNotice message={error} />
    {active && grant && <div class={s.grant}>
      <canvas ref={canvas} role="img" aria-label="新设备添加链接二维码" class={s.qr} />
      <div class={s.grantFields}>
        <Field label="添加链接"><input ref={linkInput} type="text" readOnly value={grant.enrollment_url} /></Field>
        <Button onClick={() => void copy(grant.enrollment_url, "链接", linkInput.current)}>复制链接</Button>
        <Field label="授权码"><input ref={codeInput} type="text" readOnly value={grant.authorization_code} autoComplete="off" class={s.authorizationCode} /></Field>
        <Button onClick={() => void copy(grant.authorization_code, "授权码", codeInput.current)}>复制授权码</Button>
        <p class="muted" role="timer" aria-live="off">剩余 {Math.floor(remaining / 60)}:{String(remaining % 60).padStart(2, "0")}</p>
      </div>
    </div>}
    {grant && !active && <p role="status">授权已过期，请生成新的链接和授权码。</p>}
    {copyStatus && <p role="status">{copyStatus}</p>}
    <Button onClick={() => void generate()} disabled={busy}>{busy ? "正在生成…" : grant ? "生成新的授权" : "生成链接和授权码"}</Button>
  </div>;
}
