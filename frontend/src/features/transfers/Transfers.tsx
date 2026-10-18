import { useEffect, useState } from "preact/hooks";
import {
  ApiError,
  errorMessage,
  idempotencyKey,
  request,
} from "../../api/client";
import type { ImportBatch, Operation } from "../../api/types";
import { useApp } from "../../app/context";
import { useResource } from "../../app/hooks";
import {
  Button,
  Dialog,
  ErrorNotice,
  Field,
  Icon,
  Time,
  stateLabel,
  errorCodeLabel,
  terminalState,
} from "../../components/UI";
import s from "./Transfers.module.css";
type PendingOperation = {
  kind: "backup" | "restore";
  id?: string;
  key: string;
  path: string;
  method: string;
};
const storageKey = "website-maintenance-operation";
function storedOperation(): PendingOperation | null {
  try {
    return JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
  } catch {
    return null;
  }
}
function storeOperation(value: PendingOperation | null) {
  try {
    value
      ? sessionStorage.setItem(storageKey, JSON.stringify(value))
      : sessionStorage.removeItem(storageKey);
  } catch {}
}

export function Transfers() {
  const app = useApp();
  const [restoreFile, setRestoreFile] = useState<File | null>(null);
  const importStorageKey = `website-import-${app.libraryIdentity}`;
  const [batchId, setBatchId] = useState<string | null>(() => {
    try { return sessionStorage.getItem(importStorageKey); } catch { return null; }
  });
  const workflowId = app.route.url.searchParams.get("import") ?? batchId ?? app.imports.at(-1)?.id ?? null;
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<PendingOperation | null>(
    storedOperation,
  );
  const [restoreConfirm, setRestoreConfirm] = useState(false);
  const [backupOperation, setBackupOperation] = useState<string | null>(null);
  const imports = useResource<ImportBatch>(
    workflowId ? `/imports/${workflowId}` : null,
    0,
    workflowId ? 2000 : 0,
  );
  const operation = useResource<Operation>(
    pending?.id ? `/maintenance-operations/${pending.id}` : null,
    0,
    pending?.id ? 2500 : 0,
  );
  const terminal = terminalState(operation.data?.state);
  useEffect(() => {
    if (!operation.data || !terminal) return;
    if (operation.data.state === "succeeded") {
      app.changed();
      if (operation.data.kind === "backup")
        setBackupOperation(operation.data.operation_id ?? operation.data.id!);
      app.notify(
        operation.data.kind === "restore"
          ? "收藏库已恢复，请重新读取或登录。"
          : "备份已生成，可以下载。",
      );
    }
    storeOperation(null);
  }, [operation.data?.state]);
  useEffect(() => {
    storeOperation(pending);
  }, [pending]);
  useEffect(() => {
    if (!pending || pending.id || busy) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function lookup() {
      try {
        const { data } = await request<Operation>(
          "/maintenance-operations/lookup",
          {
            method: "POST",
            body: {
              method: pending!.method,
              path: pending!.path,
              idempotency_key: pending!.key,
            },
          },
        );
        if (active)
          setPending((old) =>
            old ? { ...old, id: data.operation_id ?? data.id } : null,
          );
      } catch {
        if (active) timer = setTimeout(lookup, 5000);
      }
    }
    void lookup();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [pending?.key, pending?.id, busy]);
  async function importFile(file: File) {
    setBusy("import");
    setError("");
    try {
      const form = new FormData();
      form.set("file", file);
      const { data } = await request<ImportBatch>("/imports/preview", {
        method: "POST", body: form, key: idempotencyKey(),
      });
      setBatchId(data.id);
      app.route.params({ import: data.id }, true);
      try { sessionStorage.setItem(importStorageKey, data.id); } catch {}
      app.reloadImports();
      app.notify("解析完成，请核对工作量后确认导入。");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }
  async function confirmImport() {
    const batch = imports.data;
    if (!batch?.execution_plan?.can_execute) return;
    setBusy("import-execute"); setError("");
    try {
      const { data } = await request<ImportBatch>(`/imports/${batch.id}/execute`, {
        method: "POST", key: idempotencyKey(),
        body: { confirmation_token: batch.execution_plan.confirmation_token },
      });
      imports.setResponse({ data });
      app.reloadImports();
      app.notify("已确认导入，将按本次配额抓取并生成分类建议。");
    } catch (failure) { setError(errorMessage(failure)); }
    finally { setBusy(null); }
  }
  async function startOperation(kind: "backup" | "restore") {
    if (kind === "restore" && !restoreFile) return;
    setBusy(kind);
    setError("");
    const op: PendingOperation = {
      kind,
      key: idempotencyKey(),
      path: kind === "backup" ? "/api/v1/backups" : "/api/v1/restores/validate",
      method: "POST",
    };
    setPending(op);
    storeOperation(op);
    try {
      const form = new FormData();
      if (restoreFile) form.set("file", restoreFile);
      const { data } = await request<Operation>(
        kind === "backup" ? "/backups" : "/restores/validate",
        { method: "POST", body: kind === "backup" ? {} : form, key: op.key },
      );
      setPending({ ...op, id: data.operation_id ?? data.id });
      operation.setResponse({ data });
    } catch (e) {
      if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
        setPending(null);
        storeOperation(null);
        setError(errorMessage(e));
      } else
        setError(
          errorMessage(e) + " 正在查询该次操作的实际状态，请勿重复提交。",
        );
    } finally {
      setBusy(null);
    }
  }
  async function executeRestore() {
    if (!operation.data || !pending?.id) return;
    setBusy("restore-execute");
    setError("");
    const result = operation.data.result ?? {};
    const token = operation.data.validation_token ?? result.validation_token;
    const op = {
      ...pending,
      key: idempotencyKey(),
      path: `/api/v1/restores/${pending.id}/execute`,
      method: "POST",
    };
    setPending(op);
    storeOperation(op);
    try {
      const { data } = await request<Operation>(
        `/restores/${pending.id}/execute`,
        {
          method: "POST",
          body: { validation_token: token, confirm_overwrite: true },
          key: op.key,
        },
      );
      operation.setResponse({ data });
      setRestoreConfirm(false);
      app.notify("恢复已交给后台执行，服务可能暂时断开。");
    } catch (e) {
      setError(errorMessage(e) + " 正在查询原操作状态。");
      setRestoreConfirm(false);
    } finally {
      setBusy(null);
    }
  }
  const op = operation.data;
  const active = pending && !terminal;
  const manifest = op?.manifest ?? op?.result?.manifest;
  return (
    <main class={s.page}>
      <h1>导入与备份</h1>
      <p class="muted">
        浏览器书签只导入网站；整库备份还包含配置、目录和全部快照历史。
      </p>
      <ErrorNotice message={error} />
      <section class={s.section}>
        <h2>
          <Icon name="upload" />
          浏览器书签导入
        </h2>
        <p class="muted">
          支持 Chrome 和 Firefox 导出的 HTML
          书签文件。重复网址跳过，已有标签、备注和快照保持原样。
        </p>
        <Field label="选择浏览器 HTML 文件" hint="先预览条目和工作量，确认后导入并抓取。">
          <input type="file" accept=".html,.htm,text/html"
            disabled={!!busy || app.imports.some((batch) => ["queued", "running"].includes(batch.state))}
            onChange={(e) => {
              const file = e.currentTarget.files?.[0];
              e.currentTarget.value = "";
              if (file) void importFile(file);
            }} />
        </Field>
        {busy === "import" && <p role="status">正在上传并解析书签文件…</p>}
        {imports.data && (
          <div class={s.importResult}>
            <div class={s.stats}>
              <div>
                <strong>{imports.data.total}</strong>
                <small>总条目</small>
              </div>
              <div>
                <strong>{imports.data.valid}</strong>
                <small>可导入</small>
              </div>
              <div>
                <strong>{imports.data.duplicate}</strong>
                <small>重复跳过</small>
              </div>
              <div>
                <strong>{imports.data.invalid}</strong>
                <small>无效条目</small>
              </div>
            </div>
            {imports.data.execution_plan && <div class={s.workflow}>
              <p>确认后最多创建 {imports.data.execution_plan.captures} 个双端抓取任务，最多进行 {imports.data.execution_plan.ai_requests} 次 AI 请求（含目录规划）。</p>
              {imports.data.execution_plan.can_execute
                ? <Button primary disabled={!!busy} onClick={() => void confirmImport()}>确认导入 {imports.data.valid} 个网站</Button>
                : <p role="alert">每次最多执行 {imports.data.execution_plan.entry_limit} 个网站的抓取和分类，请拆分文件。</p>}
            </div>}
            <p>
              状态：{stateLabel(imports.data.state)}
              {imports.data.imported !== undefined
                ? ` · 已导入 ${imports.data.imported}`
                : ""}
            </p>
            <ErrorNotice message={imports.error} retry={imports.reload} />
            {!imports.data.review_ready && ["queued", "running"].includes(imports.data.state) && (
              <div aria-live="polite" class={s.workflow}>
                <p>{importStageLabel(imports.data)} · {imports.data.progress}%</p>
                <progress aria-label="导入工作流程进度" value={imports.data.progress} max={100} />
                <p class="muted">桌面与手机抓取 {imports.data.capture_completed} / {imports.data.capture_total} · 抓取失败网站 {imports.data.capture_failed_sites}</p>
              </div>
            )}
            {imports.data.review_ready && (
              <>
                <p class="muted">工作流程已完成 · 抓取失败网站 {imports.data.capture_failed_sites}
                  {imports.data.ai_status !== "succeeded" && " · 部分或全部网站未进行 AI 分类"}
                  {imports.data.ai_error_code && `（${errorCodeLabel(imports.data.ai_error_code)}）`}
                </p>
                <Button onClick={() => app.review({ importId: imports.data!.id })}>查看建议</Button>
              </>
            )}
            {imports.data.entries && (
              <details>
                <summary>查看条目与来源目录</summary>
                <div class="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>网页</th>
                        <th>原文件夹</th>
                        <th>状态</th>
                      </tr>
                    </thead>
                    <tbody>
                      {imports.data.entries.slice(0, 100).map((entry, i) => (
                        <tr key={i}>
                          <td>{entry.title || entry.url}</td>
                          <td>{entry.original_folder_path || "—"}</td>
                          <td>
                            {entry.error_code ?? stateLabel(entry.status)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
          </div>
        )}
      </section>
      <section class={s.section}>
        <h2>
          <Icon name="archive" />
          整库备份
        </h2>
        <p class="muted">
          备份包含网站、文件夹、标签、备注、配置和桌面/手机全部快照。密码、会话与
          API 密钥保存在当前设备的运行库中。
        </p>
        <Button
          primary
          icon="download"
          disabled={!!busy || !!active}
          onClick={() => void startOperation("backup")}
        >
          生成整库备份
        </Button>
        {backupOperation && (
          <a
            class={s.download}
            href={`/api/v1/backups/${backupOperation}/download`}
          >
            下载已完成的备份 <Icon name="download" size={16} />
          </a>
        )}
      </section>
      <section class={s.section}>
        <h2>
          <Icon name="refresh" />
          覆盖恢复
        </h2>
        <p class="muted">
          先校验备份，再确认覆盖。执行前会自动备份当前收藏库，恢复后需要重新登录并验证
          AI 配置。
        </p>
        <Field label="选择整库备份文件">
          <input
            type="file"
            accept=".zip,application/zip"
            disabled={!!active}
            onChange={(e) => setRestoreFile(e.currentTarget.files?.[0] ?? null)}
          />
        </Field>
        <Button
          icon="check"
          onClick={() => void startOperation("restore")}
          disabled={!restoreFile || !!busy || !!active}
        >
          上传并校验备份
        </Button>
      </section>
      {pending && (
        <section class={s.section}>
          <h2>
            <Icon name="clock" />
            {pending.kind === "backup" ? "备份进度" : "恢复进度"}
          </h2>
          <ErrorNotice message={operation.error} retry={operation.reload} />
          {operation.error && (
            <p class="muted">
              维护期间可能暂时断连，这不表示操作失败。保留原操作编号，重新登录后继续查询。
            </p>
          )}
          {!pending.id ? (
            <p class="muted">正在查找原操作，请勿重复提交…</p>
          ) : op ? (
            <>
              <p class={op.state === "failed" ? "status-bad" : ""}>
                {stateLabel(op.state)} · {stageName(op.stage)}
              </p>
              <small>
                <Time value={op.created_at} />
              </small>
              {op.byte_size && <p>文件大小：{formatBytes(op.byte_size)}</p>}
              {op.error_code && (
                <p class="status-bad">{errorCodeLabel(op.error_code)}</p>
              )}
              {manifest && (
                <ManifestSummary
                  manifest={manifest as Record<string, unknown>}
                />
              )}
              {op.state === "awaiting_confirmation" && (
                <div class={s.warning}>
                  <p>
                    备份校验通过。恢复将覆盖当前库，包括现有网站、目录与全部快照历史。
                  </p>
                  <Button
                    danger
                    disabled={
                      !!busy ||
                      !(op.validation_token ?? op.result?.validation_token)
                    }
                    onClick={() => setRestoreConfirm(true)}
                  >
                    确认覆盖恢复
                  </Button>
                  <Button
                    disabled={!!busy}
                    onClick={() => void (async () => {
                      setBusy("restore-cancel"); setError("");
                      try {
                        await request(`/restores/${pending.id}/cancel`, { method: "POST", body: {} });
                        setPending(null); storeOperation(null);
                      } catch (failure) { setError(errorMessage(failure)); }
                      finally { setBusy(null); }
                    })()}
                  >
                    取消本次恢复
                  </Button>
                </div>
              )}
              {op.state === "succeeded" && op.kind === "backup" && (
                <a
                  class={s.download}
                  href={`/api/v1/backups/${pending.id}/download`}
                >
                  下载整库备份 <Icon name="download" size={16} />
                </a>
              )}
              {op.state === "succeeded" && op.kind === "restore" && (
                <Button
                  onClick={() => {
                    app.changed();
                    app.route.navigate("/library");
                  }}
                >
                  查看恢复后的收藏库
                </Button>
              )}
              {op.kind === "restore" && !!op.result?.pre_restore_backup && (
                <a
                  class={s.download}
                  href={`/api/v1/restores/${pending.id}/pre-backup/download`}
                >
                  下载恢复前的自动备份 <Icon name="download" size={16} />
                </a>
              )}
              {terminal && (
                <Button
                  onClick={() => {
                    setPending(null);
                    storeOperation(null);
                  }}
                >
                  关闭进度记录
                </Button>
              )}
            </>
          ) : (
            <p>正在读取操作状态…</p>
          )}
        </section>
      )}
      {restoreConfirm && (
        <Dialog
          title="确认覆盖当前收藏库"
          onClose={() => setRestoreConfirm(false)}
        >
          <p>
            此操作会使用已校验的备份完整替换当前收藏库。系统会先备份当前库，再执行恢复。
          </p>
          <p class="status-bad">
            备份之后新增或修改的记录将被所选备份中的内容覆盖。
          </p>
          <div class="form-actions">
            <Button onClick={() => setRestoreConfirm(false)}>取消</Button>
            <Button
              danger
              onClick={() => void executeRestore()}
              disabled={!!busy}
            >
              覆盖恢复
            </Button>
          </div>
        </Dialog>
      )}
    </main>
  );
}
function formatBytes(value: number) {
  return value >= 1048576
    ? (value / 1048576).toFixed(1) + " MiB"
    : value >= 1024
      ? (value / 1024).toFixed(1) + " KiB"
      : value + " B";
}
function stageName(stage: string) {
  return (
    (
      {
        queued: "等待执行",
        validating: "检查清单与文件校验",
        validated: "等待确认",
        awaiting_confirmation: "等待覆盖确认",
        freezing: "暂停库写入并建立一致性副本",
        packing: "打包收藏库",
        backup: "备份当前库",
        pre_backup: "备份当前库",
        restoring: "恢复收藏库",
        switching: "切换收藏库",
        checking: "验证恢复结果",
        completed: "完成",
        done: "完成",
      } as Record<string, string>
    )[stage] ?? stage
  );
}

function ManifestSummary({ manifest }: { manifest: Record<string, unknown> }) {
  const counts = (manifest.counts ?? {}) as Record<string, unknown>;
  return (
    <div class={s.manifest}>
      {typeof manifest.created_at === "string" && (
        <p>
          <span>备份时间</span>
          <Time value={manifest.created_at} />
        </p>
      )}
      {[
        { key: "bookmarks", name: "网站" },
        { key: "folders", name: "文件夹" },
        { key: "desktop_snapshots", name: "桌面快照" },
        { key: "mobile_snapshots", name: "手机快照" },
        { key: "snapshots", name: "全部快照" },
      ]
        .filter((item) => counts[item.key] !== undefined)
        .map((item) => (
          <p key={item.key}>
            <span>{item.name}</span>
            <strong>{String(counts[item.key])}</strong>
          </p>
        ))}
    </div>
  );
}

function importStageLabel(batch: ImportBatch) {
  if (batch.stage === "captures") return "正在抓取双端快照";
  if (batch.stage === "classification") return `正在进行 AI 分类 ${batch.classification_completed} / ${batch.classification_total}`;
  return "正在导入网站记录";
}
