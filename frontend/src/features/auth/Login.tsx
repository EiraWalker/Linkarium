import { useEffect, useRef, useState } from "preact/hooks";
import { startAuthentication, startRegistration } from "@simplewebauthn/browser";
import type { PublicKeyCredentialCreationOptionsJSON, PublicKeyCredentialRequestOptionsJSON } from "@simplewebauthn/browser";
import { errorMessage, getSession, request } from "../../api/client";
import { Button, ErrorNotice, Field, Icon } from "../../components/UI";
import s from "../../app/App.module.css";

const encode = (buffer: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(buffer)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export function Login({ onLogin, initializationRequired, maintenanceMode, passwordEnabled = false }: {
  onLogin: () => Promise<void>; initializationRequired: boolean;
  maintenanceMode: boolean; passwordEnabled?: boolean;
}) {
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [setupToken] = useState(() => new URLSearchParams(location.hash.slice(1)).get("passkey-setup") ?? "");
  const fileInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (setupToken) history.replaceState(history.state, "", location.pathname + location.search);
  }, []);

  async function perform(action: () => Promise<void>) {
    setError(""); setBusy(true);
    try { await getSession(); await action(); await onLogin(); }
    catch (e) {
      setError(e instanceof Error && ["NotAllowedError", "AbortError"].includes(e.name)
        ? "认证已取消，请重试。" : errorMessage(e));
    } finally { setBusy(false); }
  }
  async function passkey() {
    await perform(async () => {
      if (!window.isSecureContext || !window.PublicKeyCredential)
        throw new Error("请通过 HTTPS 或 localhost 使用支持 Passkey 的浏览器。");
      if (setupToken) {
        const { data } = await request<{ options: PublicKeyCredentialCreationOptionsJSON; challenge_id: string }>(
          "/session/passkey/register/options", { method: "POST", body: { setup_token: setupToken } });
        const credential = await startRegistration({ optionsJSON: data.options });
        await request("/session/passkey/register/verify", { method: "POST", body: { credential, challenge_id: data.challenge_id } });
      } else {
        const { data } = await request<{ options: PublicKeyCredentialRequestOptionsJSON; challenge_id: string }>(
          "/session/passkey/login/options", { method: "POST", body: {} });
        const credential = await startAuthentication({ optionsJSON: data.options });
        await request("/session/passkey/login/verify", { method: "POST", body: { credential, challenge_id: data.challenge_id } });
      }
    });
  }
  async function privateKey(file: File) {
    await perform(async () => {
      if (!window.isSecureContext || !crypto.subtle) throw new Error("请通过 HTTPS 或 localhost 登录。");
      if (file.size > 16384) throw new Error("请选择 Ed25519 PKCS8 PEM 私钥文件。");
      const pem = await file.text();
      const match = /^\s*-----BEGIN PRIVATE KEY-----\s+([A-Za-z0-9+/=\s]+)\s+-----END PRIVATE KEY-----\s*$/.exec(pem);
      if (!match) throw new Error("请选择 Ed25519 PKCS8 PEM 私钥文件。");
      const bytes = Uint8Array.from(atob(match[1].replace(/\s/g, "")), c => c.charCodeAt(0));
      let key: CryptoKey;
      try { key = await crypto.subtle.importKey("pkcs8", bytes, { name: "Ed25519" }, false, ["sign"]); }
      catch { throw new Error("私钥格式不支持，请使用软件生成的 Ed25519 PEM 文件。"); }
      finally { bytes.fill(0); }
      const { data } = await request<{ challenge_id: string; message: string }>("/session/key/options", { method: "POST", body: {} });
      const signature = await crypto.subtle.sign("Ed25519", key, new TextEncoder().encode(data.message));
      await request("/session/key/verify", { method: "POST", body: { challenge_id: data.challenge_id, signature: encode(signature) } });
    });
    if (fileInput.current) fileInput.current.value = "";
  }

  return <main class={s.login}><div class={s.loginPanel}>
    <div class={s.loginBrand}><Icon name="bookmark" size={36} /><h1>Linkarium</h1><p>让每一个好网站，都有它的位置。</p></div>
    {maintenanceMode ? <p role="status">正在维护数据，请等待完成后重新连接。</p>
      : initializationRequired ? <div><p role="status">请先在部署电脑初始化访问认证。</p>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: "12px" }}>.\.venv\Scripts\python.exe backend\manage.py initialize{passwordEnabled ? " --debug" : ""}</pre>
        <Button primary onClick={() => void onLogin()}>重新检查</Button></div>
      : <div>
        <ErrorNotice message={error} />
        {passwordEnabled && !setupToken && <form onSubmit={e => { e.preventDefault(); void perform(async () => {
          await request("/session/login", { method: "POST", body: { password } }); setPassword("");
        }); }}>
          <Field label="调试密码"><div class={s.passwordInput}><input type="password" aria-label="调试密码" ref={node => { if (node) node.type = show ? "text" : "password"; }}
            autoComplete="current-password" required minLength={6} value={password}
            onInput={e => setPassword(e.currentTarget.value)} placeholder="输入调试密码" />
            <button type="button" onClick={() => setShow(v => !v)}>{show ? "隐藏" : "显示"}</button></div></Field>
          <Button primary type="submit" disabled={busy} className={s.loginButton}>密码登录</Button>
        </form>}
        <div style={{ display: "grid", gap: "12px", marginTop: "12px" }}>
          <Button primary={!passwordEnabled || !!setupToken} disabled={busy} onClick={() => void passkey()}>
            {busy ? "正在认证…" : setupToken ? "绑定 Passkey" : "Passkey 登录"}</Button>
          {!setupToken && <Button disabled={busy} onClick={() => fileInput.current?.click()}>私钥文件登录</Button>}
          <input ref={fileInput} type="file" accept=".pem" hidden onChange={e => {
            const file = e.currentTarget.files?.[0]; if (file) void privateKey(file);
          }} />
        </div>
      </div>}
  </div></main>;
}
