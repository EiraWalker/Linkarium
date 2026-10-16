import { useEffect, useState } from "preact/hooks";
import { ApiError, errorMessage, idempotencyKey, request } from "../../api/client";
import type { Operation } from "../../api/types";
import { useApp } from "../../app/context";
import { useResource } from "../../app/hooks";
import { Button, Dialog, ErrorNotice, Field, Icon, terminalState } from "../../components/UI";
import s from "./Settings.module.css";

type Action = "clear" | "reset";
type Pending = { kind: Action; key: string; epoch: number; id?: string };
const labels = { clear: "清空仓库", reset: "重置数据" };
const storageKey = "website-data-operation";
function storedPending(): Pending | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
    return value && (value.kind === "clear" || value.kind === "reset") && typeof value.key === "string" && typeof value.epoch === "number" ? value : null;
  } catch { return null; }
}
function storePending(value: Pending | null) {
  try {
    value ? sessionStorage.setItem(storageKey, JSON.stringify(value)) : sessionStorage.removeItem(storageKey);
  } catch {}
}

export function DataActions({ libraryEpoch }: { libraryEpoch?: number }) {
  const app = useApp();
  const [action, setAction] = useState<Action | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(storedPending);
  const operation = useResource<Operation>(pending?.id ? `/maintenance-operations/${pending.id}` : null, 0, pending?.id ? 2000 : 0);
  useEffect(() => { storePending(pending); }, [pending]);
  useEffect(() => {
    if (!operation.data || !terminalState(operation.data.state)) return;
    storePending(null);
    setPending(null);
    if (operation.data.state === "succeeded") {
      if (pending?.kind === "reset") {
        clearBrowserState();
        location.replace("/login");
      } else location.replace("/settings");
    } else setError("操作未完成，请检查运行状态后重试。原仓库在开始删除之前的失败中会保留。");
  }, [operation.data?.state]);
  useEffect(() => {
    if (!pending || pending.id || busy) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function lookup() {
      try {
        const { data } = await request<Operation>("/maintenance-operations/lookup", {
          method: "POST", signal: controller.signal,
          body: { method: "POST", path: `/api/v1/data/${pending!.kind}`, idempotency_key: pending!.key },
        });
        if (!controller.signal.aborted) setPending(old => old ? { ...old, id: data.operation_id } : null);
      } catch (e) {
        if (controller.signal.aborted) return;
        if (e instanceof ApiError && e.status === 404) {
          try {
            // A lost registration response can race lookup; retry only the same confirmed request.
            const { data } = await request<Operation>(`/data/${pending!.kind}`, {
              method: "POST", signal: controller.signal, key: pending!.key,
              body: { confirmation: labels[pending!.kind], library_epoch: pending!.epoch },
            });
            if (!controller.signal.aborted) setPending(old => old ? { ...old, id: data.operation_id } : null);
          } catch (retryError) {
            if (controller.signal.aborted) return;
            if (retryError instanceof ApiError && retryError.status >= 400 && retryError.status < 500) {
              setError(errorMessage(retryError));
              setPending(null);
            } else timer = setTimeout(lookup, 3000);
          }
        } else timer = setTimeout(lookup, 3000);
      }
    }
    void lookup();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [pending?.key, pending?.id, busy]);
  function open(kind: Action) {
    setAction(kind);
    setConfirmation("");
    setError("");
  }
  async function execute(e: Event) {
    e.preventDefault();
    if (!action || confirmation !== labels[action] || libraryEpoch === undefined || busy || pending) return;
    setBusy(true);
    setError("");
    const next: Pending = { kind: action, key: idempotencyKey(), epoch: libraryEpoch };
    storePending(next);
    setPending(next);
    try {
      const { data } = await request<Operation>(`/data/${action}`, {
        method: "POST", key: next.key,
        body: { confirmation, library_epoch: libraryEpoch },
      });
      const accepted = { ...next, id: data.operation_id };
      storePending(accepted);
      setPending(accepted);
      setAction(null);
      window.dispatchEvent(new Event("service-maintenance"));
      app.notify(action === "clear" ? "正在清空仓库，完成后页面会自动刷新。" : "正在重置数据，完成后需要在本机重新设置登录密码。");
    } catch (e) {
      if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
        storePending(null);
        setPending(null);
      } else setAction(null);
      setError(errorMessage(e));
    } finally { setBusy(false); }
  }
  return (
    <section class={`${s.section} ${s.dataSection}`}>
      <h2><Icon name="trash" />数据管理</h2>
      <ErrorNotice message={error} />
      {pending && (
        <p role="status" class="muted">
          {labels[pending.kind]}正在处理，服务可能暂时断开。请等待完成。
          {operation.error && " 正在重新连接并查询原操作。"}
        </p>
      )}
      <div class={s.dataAction}>
        <div><h3>清空仓库</h3><p>删除所有文件夹、网页、标签、快照历史、分类建议、任务和应用内备份。保留登录密码、刷新设置及 AI 配置与密钥。</p></div>
        <Button danger disabled={busy || !!pending || libraryEpoch === undefined} onClick={() => open("clear")}>清空仓库</Button>
      </div>
      <div class={s.dataAction}>
        <div><h3>重置数据</h3><p>删除全部用户数据，包括仓库、应用内备份、所有设置、登录密码、会话和 AI 密钥。完成后需要在本机重新初始化登录密码。</p></div>
        <Button danger disabled={busy || !!pending || libraryEpoch === undefined} onClick={() => open("reset")}>重置数据</Button>
      </div>
      {action && (
        <Dialog title={labels[action]} onClose={() => { if (!busy) setAction(null); }}>
          <form onSubmit={execute}>
            <p class="status-bad">此操作无法撤销，应用保存的旧备份也会删除。需要保留的数据请先下载备份。</p>
            <p>{action === "reset" ? "登录密码和 AI 密钥也会删除，所有设备都会退出登录。请确认你可以在运行软件的电脑上重新初始化密码。" : "仓库中的所有网站数据都会删除，登录密码与设置将保留。"}</p>
            <ErrorNotice message={error} />
            <Field label={`输入“${labels[action]}”以确认`}>
              <input autoFocus required autoComplete="off" disabled={busy} value={confirmation} onInput={e => setConfirmation(e.currentTarget.value)} />
            </Field>
            <div class="form-actions">
              <Button disabled={busy} onClick={() => setAction(null)}>取消</Button>
              <Button danger type="submit" disabled={busy || !!pending || confirmation !== labels[action]}>{busy ? "正在提交…" : `确认${labels[action]}`}</Button>
            </div>
          </form>
        </Dialog>
      )}
    </section>
  );
}

export function clearBrowserState() {
  for (const storage of [sessionStorage, localStorage]) {
    try {
      for (const key of Object.keys(storage)) if (key.startsWith("website-")) storage.removeItem(key);
    } catch {}
  }
}
