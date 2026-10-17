import { useState } from "preact/hooks";
import { getSession, request } from "../../api/client";
import type { NetworkConnection, PasskeyInventory, PasskeyRecord } from "../../api/types";
import { useApp } from "../../app/context";
import { useResource } from "../../app/hooks";
import { Button, Dialog, ErrorNotice, Field, Icon, Time } from "../../components/UI";
import { PasskeyEnrollment } from "../auth/PasskeyEnrollment";
import { passkeyError } from "../auth/passkeyClient";
import { DeviceEnrollment } from "./DeviceEnrollment";
import s from "./Settings.module.css";
import p from "./Passkeys.module.css";

export function PasskeySettings({ connection, connectionReady = true }: {
  connection?: NetworkConnection | null;
  connectionReady?: boolean;
}) {
  const app = useApp();
  const inventory = useResource<PasskeyInventory>("/session/passkeys", app.refresh);
  const [editing, setEditing] = useState<PasskeyRecord | null>(null);
  const [name, setName] = useState("");
  const [deleting, setDeleting] = useState<PasskeyRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [registrationKey, setRegistrationKey] = useState(0);

  async function rename(event: Event) {
    event.preventDefault();
    if (!editing || busy) return;
    setBusy(true); setError("");
    try {
      await request(`/session/passkeys/${encodeURIComponent(editing.id)}`, { method: "PATCH", body: { name: name.trim() } });
      setEditing(null); inventory.reload(); app.notify("Passkey 名称已更新。");
    } catch (failure) { setError(passkeyError(failure)); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (!deleting || busy) return;
    setBusy(true); setDeleteError("");
    try {
      const { data } = await request<{ deleted: boolean; logged_out: boolean }>(
        `/session/passkeys/${encodeURIComponent(deleting.id)}`, { method: "DELETE" },
      );
      setDeleting(null);
      if (data.logged_out) {
        window.dispatchEvent(new Event("session-expired"));
        return;
      }
      inventory.reload(); app.notify("Passkey 已删除。");
    } catch (failure) { setDeleteError(passkeyError(failure)); inventory.reload(); }
    finally { setBusy(false); }
  }

  return <section class={s.section}>
    <h2><Icon name="settings" />登录与 Passkey</h2>
    <p class="muted">登录保持 14 天。Passkey 可由密码管理器同步到多台设备。</p>
    <ErrorNotice message={inventory.error} retry={inventory.reload} />
    <ErrorNotice message={error} />
    {inventory.loading && <p role="status">正在加载 Passkey…</p>}
    {inventory.data && !inventory.data.items.length && <p class="muted">尚未添加 Passkey。</p>}
    <ul class={p.list}>
      {inventory.data?.items.map(item => <li key={item.id} class={p.item}>
        {editing?.id === item.id ? <form class={p.rename} onSubmit={rename}>
          <Field label="新名称"><input type="text" value={name} required maxLength={100} autoFocus disabled={busy}
            onInput={event => setName(event.currentTarget.value)} /></Field>
          <div class={p.actions}>
            <Button primary type="submit" disabled={busy || !name.trim()}>保存名称</Button>
            <Button disabled={busy} onClick={() => { setEditing(null); setError(""); }}>取消</Button>
          </div>
        </form> : <div class={p.row}>
          <div>
            <h3 class={p.name}>{item.name}{item.is_current && <span class={p.current}>当前登录</span>}</h3>
            <div class={p.metadata}>
              <span>{item.rp_id}</span>
              <span>创建于 <Time value={item.created_at} /></span>
              <span>最后使用 {item.last_used_at ? <Time value={item.last_used_at} /> : "尚未使用"}</span>
            </div>
          </div>
          <div class={p.actions}>
            <Button disabled={busy} onClick={() => { setEditing(item); setName(item.name); setError(""); }}>重命名</Button>
            <Button danger disabled={busy || !item.can_delete} title={!item.can_delete ? "最后一种登录凭据不能删除" : undefined}
              onClick={() => { setDeleting(item); setDeleteError(""); }}>删除</Button>
          </div>
        </div>}
        {!item.can_delete && <p class={p.lastCredential}>添加另一种登录凭据后，才能删除此 Passkey。</p>}
      </li>)}
    </ul>
    {inventory.data?.can_add ? <>
      <div class={p.local}>
        <h3>在本机添加</h3>
        <PasskeyEnrollment key={registrationKey} mode="local" onComplete={async () => {
          await getSession(); inventory.reload(); setRegistrationKey(value => value + 1); app.notify("Passkey 已添加。");
        }} />
      </div>
    </> : inventory.data && <p class="muted">使用私钥文件或 Passkey 登录后，可在这里添加 Passkey。</p>}
    {connectionReady && (connection || inventory.data?.can_add) &&
      <DeviceEnrollment connection={connection} canAuthorize={inventory.data?.can_add ?? false} />}
    {deleting && <Dialog title="删除 Passkey" onClose={() => { if (!busy) setDeleting(null); }}>
      <p>删除“{deleting.name}”并退出使用它登录的所有会话？</p>
      {deleting.is_current && <p>当前页面也会退出登录。</p>}
      <ErrorNotice message={deleteError} />
      <div class={p.actions}>
        <Button danger disabled={busy} onClick={() => void remove()}>{busy ? "正在删除…" : "确认删除"}</Button>
        <Button disabled={busy} onClick={() => setDeleting(null)}>取消</Button>
      </div>
    </Dialog>}
  </section>;
}
