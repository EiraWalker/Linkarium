import { useRef, useState } from "preact/hooks";
import { getSession, request } from "../../api/client";
import { Button, ErrorNotice, Field, Icon } from "../../components/UI";
import { PasskeyEnrollment } from "./PasskeyEnrollment";
import { clearEnrollmentIntent, loginWithPasskey, passkeyError, type EnrollmentIntent } from "./passkeyClient";
import s from "../../app/App.module.css";
import p from "./PasskeyEnrollment.module.css";

const encode = (buffer: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(buffer)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export function Login({ onLogin, initializationRequired, maintenanceMode, passwordEnabled = false,
  enrollmentIntent = null, passkeyOrigin, passkeyAvailable = true, passkeyConfigured = true,
}: {
  onLogin: (notice?: string) => Promise<void>;
  initializationRequired: boolean;
  maintenanceMode: boolean;
  passwordEnabled?: boolean;
  enrollmentIntent?: EnrollmentIntent | null;
  passkeyOrigin?: string | null;
  passkeyAvailable?: boolean;
  passkeyConfigured?: boolean;
}) {
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ordinaryLogin, setOrdinaryLogin] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  const intent = ordinaryLogin ? null : enrollmentIntent;
  const bootstrap = initializationRequired && !ordinaryLogin && !intent;
  const firstPasskey = !initializationRequired && !passkeyConfigured && !ordinaryLogin && !intent;
  const canonicalOrigin = (() => {
    try {
      const url = new URL(passkeyOrigin ?? location.origin);
      return ["http:", "https:"].includes(url.protocol) ? url.origin : null;
    } catch { return null; }
  })();
  const wrongOrigin = !!canonicalOrigin && canonicalOrigin !== location.origin;
  const canonicalHref = canonicalOrigin ? `${canonicalOrigin}/login${intent && intent.kind !== "obsolete"
    ? `#${intent.kind}=${encodeURIComponent(intent.token)}` : ""}` : null;
  const canUsePasskey = passkeyAvailable && !wrongOrigin;

  async function perform(action: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setError(""); setBusy(true);
    try { await getSession(); await action(); await onLogin(); }
    catch (failure) { setError(passkeyError(failure)); }
    finally { inFlight.current = false; setBusy(false); }
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

  const openLogin = () => { clearEnrollmentIntent(); setOrdinaryLogin(true); setError(""); };

  return <main class={s.login}><div class={s.loginPanel}>
    <div class={s.loginBrand}><Icon name="bookmark" size={36} /><h1>Linkarium</h1><p>让每一个好网站，都有它的位置。</p></div>
    {maintenanceMode ? <p role="status">正在维护数据，请等待完成后重新连接。</p>
      : bootstrap && passwordEnabled ? <div>
        <p role="status">请先在部署电脑初始化调试账号。</p>
        <Button primary onClick={() => void onLogin()}>重新检查</Button>
      </div> : <div>
        {!canUsePasskey && <p class={p.status} role="status">{wrongOrigin && canonicalHref
          ? <>请通过 <a href={canonicalHref}>网站配置的地址</a> 使用 Passkey。</>
          : "Passkey 暂不可用，请在服务器配置 HTTPS 访问地址。"}</p>}
        {intent?.kind === "obsolete" ? <div>
          <p role="alert">此添加入口已失效，请重新获取链接和授权码。</p>
          <Button onClick={openLogin}>返回登录</Button>
        </div> : bootstrap || firstPasskey || intent ? <div>
          <h2 class={p.heading}>{bootstrap ? "首次配置" : firstPasskey ? "创建第一个 Passkey" : intent?.kind === "reset" ? "重置登录凭据" : "添加此设备的 Passkey"}</h2>
          {intent?.kind === "reset" && <p class={p.status}>新 Passkey 创建成功后，旧 Passkey、私钥文件登录凭据和登录会话将失效。</p>}
          <PasskeyEnrollment mode={bootstrap ? "bootstrap" : firstPasskey ? "first_passkey" : intent!} disabled={!canUsePasskey}
            onComplete={async result => { await onLogin(result.registration_mode === "reset" ? "旧登录凭据已撤销。" : undefined); }} onLogin={openLogin} />
          <div class={p.back}><Button onClick={openLogin}>返回登录</Button></div>
        </div> : <div>
          <ErrorNotice message={error} />
          {passwordEnabled && <form onSubmit={event => {
            event.preventDefault();
            void perform(async () => {
              await request("/session/login", { method: "POST", body: { password } }); setPassword("");
            });
          }}>
            <Field label="调试密码"><div class={s.passwordInput}>
              <input type="password" ref={node => { if (node) node.type = show ? "text" : "password"; }} aria-label="调试密码" autoComplete="current-password"
                required minLength={6} value={password} disabled={busy} onInput={event => setPassword(event.currentTarget.value)} placeholder="输入调试密码" />
              <button type="button" onClick={() => setShow(value => !value)}>{show ? "隐藏" : "显示"}</button>
            </div></Field>
            <Button primary type="submit" disabled={busy} className={s.loginButton}>密码登录</Button>
          </form>}
          <div class={p.actions}>
            <Button primary={!passwordEnabled} disabled={busy || !canUsePasskey} onClick={() => void perform(loginWithPasskey)}>
              {busy ? "正在认证…" : "Passkey 登录"}
            </Button>
            <Button disabled={busy} onClick={() => fileInput.current?.click()}>私钥文件登录</Button>
            {(initializationRequired || !passkeyConfigured) && <Button onClick={() => setOrdinaryLogin(false)}>{initializationRequired ? "首次配置" : "创建第一个 Passkey"}</Button>}
            <input ref={fileInput} type="file" accept=".pem" hidden onChange={event => {
              const file = event.currentTarget.files?.[0]; if (file) void privateKey(file);
            }} />
          </div>
        </div>}
      </div>}
  </div></main>;
}
