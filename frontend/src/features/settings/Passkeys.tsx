import { useState } from "preact/hooks";
import { startRegistration, type PublicKeyCredentialCreationOptionsJSON } from "@simplewebauthn/browser";
import { errorMessage, getSession, request } from "../../api/client";
import { useApp } from "../../app/context";
import { useResource } from "../../app/hooks";
import { Button, ErrorNotice, Field, Icon, Time } from "../../components/UI";
import s from "./Settings.module.css";

interface PasskeyInventory {
  can_add: boolean;
  items: { id: string; name: string; rp_id: string; created_at: string; last_used_at: string | null }[];
}

export function PasskeySettings() {
  const app = useApp();
  const inventory = useResource<PasskeyInventory>("/session/passkeys", app.refresh);
  const [name, setName] = useState("这台设备");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function add(e: Event) {
    e.preventDefault();
    setError(""); setBusy(true);
    try {
      if (!window.isSecureContext || !window.PublicKeyCredential)
        throw new Error("请通过 HTTPS 或 localhost 使用支持 Passkey 的浏览器。");
      const { data } = await request<{ options: PublicKeyCredentialCreationOptionsJSON; challenge_id: string }>(
        "/session/passkey/register/options", { method: "POST", body: {} });
      const credential = await startRegistration({ optionsJSON: data.options });
      await request("/session/passkey/register/verify", { method: "POST", body: { credential, name: name.trim(), challenge_id: data.challenge_id } });
      await getSession();
      inventory.reload();
      app.notify("Passkey 已添加。");
    } catch (e) { setError(errorMessage(e)); }
    finally { setBusy(false); }
  }
  return <section class={s.section}>
    <h2><Icon name="settings" />登录与 Passkey</h2>
    <ErrorNotice message={inventory.error} retry={inventory.reload} />
    <ErrorNotice message={error} />
    {inventory.data?.items.map(item => <p key={item.id}>
      <strong>{item.name}</strong><span class="muted"> · {item.rp_id} · <Time value={item.created_at} /></span>
    </p>)}
    {inventory.data?.can_add ? <form onSubmit={add}>
      <Field label="Passkey 名称"><input type="text" required maxLength={100} value={name}
        onInput={e => setName(e.currentTarget.value)} /></Field>
      <Button primary type="submit" icon="plus" disabled={busy || !name.trim()}>{busy ? "正在绑定…" : "添加 Passkey"}</Button>
    </form> : inventory.data && <p class="muted">使用私钥文件或 Passkey 登录后，可在这里添加 Passkey。</p>}
  </section>;
}
