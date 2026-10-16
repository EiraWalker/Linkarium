import { useEffect, useRef, useState } from "preact/hooks";
import {
  errorMessage,
  idempotencyKey,
  queryString,
  request,
  safeUrl,
} from "../../api/client";
import type { Entity, Proposal } from "../../api/types";
import { folderPath, useApp, type ReviewScope } from "../../app/context";
import { useResource } from "../../app/hooks";
import {
  Button,
  Dialog,
  Empty,
  ErrorNotice,
  Field,
  Icon,
  stateLabel,
} from "../../components/UI";
import s from "./Classification.module.css";

export function ClassificationReview({ scope, onClose }: { scope: ReviewScope; onClose: () => void }) {
  const app = useApp();
  const [status, setStatus] = useState("pending");
  const [page, setPage] = useState(1);
  const [checked, setChecked] = useState<Map<string, Entity>>(new Map());
  const [selectingAll, setSelectingAll] = useState(false);
  const selectRequest = useRef<AbortController | null>(null);
  const allRef = useRef<HTMLInputElement>(null);
  const pageRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [edit, setEdit] = useState<Proposal | null>(null);
  const selectionQuery = queryString({ status, import_id: scope.importId, bookmark_id: scope.bookmarkId, group_id: scope.groupId });
  const selectionScope = `${app.libraryIdentity}:${selectionQuery}`;
  const currentScope = useRef(selectionScope);
  currentScope.current = selectionScope;
  useEffect(() => {
    selectRequest.current?.abort();
    setSelectingAll(false);
    setChecked(new Map());
    setPage(1);
    return () => selectRequest.current?.abort();
  }, [selectionScope]);
  const proposals = useResource<Proposal[]>(
    `/classification/proposals?${queryString({ status, page, page_size: 48, import_id: scope.importId, bookmark_id: scope.bookmarkId, group_id: scope.groupId })}`,
    app.refresh,
    10000,
  );
  const items = proposals.data ?? [];
  const total = proposals.page?.total ?? items.length;
  const pageSelected = items.length > 0 && items.every((p) => checked.has(p.id));
  const allSelected = checked.size > 0 && checked.size === total && pageSelected;
  useEffect(() => {
    if (allRef.current) allRef.current.indeterminate = checked.size > 0 && !allSelected;
    if (pageRef.current) pageRef.current.indeterminate = !pageSelected && items.some((p) => checked.has(p.id));
  }, [checked, items, allSelected, pageSelected]);
  async function selectAllPages() {
    if (allSelected) { setChecked(new Map()); return; }
    const controller = new AbortController();
    selectRequest.current?.abort();
    selectRequest.current = controller;
    setSelectingAll(true);
    setError("");
    try {
      const { data } = await request<Entity[]>(`/classification/proposals?${selectionQuery}&selection=true`, { signal: controller.signal });
      if (!controller.signal.aborted && currentScope.current === selectionScope)
        setChecked(new Map(data.map((item) => [item.id, item])));
    } catch (e) {
      if (!controller.signal.aborted && currentScope.current === selectionScope) setError(errorMessage(e));
    } finally {
      if (!controller.signal.aborted && currentScope.current === selectionScope) setSelectingAll(false);
    }
  }
  async function decide(action: "accept" | "reject") {
    setBusy(true);
    setError("");
    try {
      await request(`/classification/${action}`, {
        method: "POST",
        body: {
          items: Array.from(checked.values()),
          tree_revision: app.settings?.tree_revision,
        },
        key: idempotencyKey(),
      });
      setChecked(new Map());
      setPage(1);
      app.changed();
      app.notify(
        action === "accept"
          ? "已确认分类，文件夹和网站归属已更新。"
          : "已驳回建议，网站保持当前归属。",
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="分类校对" onClose={onClose} wide>
      <p class="muted">先审阅建议，确认后再创建文件夹和移动网站。未进行 AI 分类的条目可人工修改目标。</p>
      <div class={s.toolbar}>
        <select
          aria-label="分类状态"
          disabled={busy || selectingAll}
          value={status}
          onChange={(e) => {
            setStatus(e.currentTarget.value);
            setPage(1);
            setChecked(new Map());
          }}
        >
          <option value="pending">待确认</option>
          <option value="stale">已过期</option>
          <option value="accepted">已确认</option>
          <option value="rejected">已驳回</option>
        </select>
        <Button icon="refresh" disabled={busy || selectingAll} onClick={() => { setChecked(new Map()); proposals.reload(); }}>
          刷新列表
        </Button>
        {status === "pending" && checked.size > 0 && (
          <>
            <span class="muted">已选 {checked.size} 项</span>
            <Button
              primary
              disabled={busy || selectingAll}
              onClick={() => void decide("accept")}
              icon="check"
            >
              {allSelected ? "全部批准" : "确认选中"}
            </Button>
            <Button disabled={busy || selectingAll} onClick={() => void decide("reject")}>
              驳回选中
            </Button>
          </>
        )}
      </div>
      <ErrorNotice
        message={error || proposals.error}
        retry={proposals.error ? proposals.reload : undefined}
      />
      {!items.length ? (
        <Empty
          title={
            proposals.loading
              ? "正在读取分类建议…"
              : "暂无" + stateLabel(status) + "的分类建议"
          }
        >
          <p>
            此范围内暂无分类建议。可以在网站详情主动生成建议，也可以编辑网站的文件夹归属。
          </p>
          <Button onClick={() => app.route.navigate("/settings")}>
            配置 AI
          </Button>
        </Empty>
      ) : (
        <>
          <div class={s.selectAll}>
            <label>
              <input
                  type="checkbox"
                  ref={pageRef}
                  disabled={status !== "pending" || busy || selectingAll}
                  checked={pageSelected}
                  onChange={() => setChecked((current) => {
                    const next = new Map(current);
                    for (const item of items) {
                      if (pageSelected) next.delete(item.id);
                      else next.set(item.id, { id: item.id, revision: item.revision });
                    }
                    return next;
                  })}
              />
                选择当前页
              </label>
              <label>
                <input type="checkbox" ref={allRef} disabled={status !== "pending" || busy || selectingAll}
                  checked={allSelected} onChange={() => void selectAllPages()} />
                全选所有页
              </label>
              {selectingAll && <span>正在选择所有页…</span>}
          </div>
          <div class={s.proposals}>
            {items.map((proposal) => (
              <article class={s.proposal} key={proposal.id}>
                <label class={s.checkbox}>
                  <input
                    type="checkbox"
                    aria-label={`选择建议 ${proposal.bookmark_title ?? proposal.bookmark?.title ?? proposal.title ?? proposal.bookmark_id}`}
                    disabled={proposal.status !== "pending" || busy || selectingAll}
                    checked={checked.has(proposal.id)}
                    onChange={() =>
                      setChecked((current) => {
                        const next = new Map(current);
                        next.has(proposal.id)
                          ? next.delete(proposal.id)
                          : next.set(proposal.id, { id: proposal.id, revision: proposal.revision });
                        return next;
                      })
                    }
                  />
                </label>
                <div class={s.proposalContent}>
                  <div class={s.proposalHeading}>
                    <h2>
                      {proposal.bookmark_title ??
                        proposal.bookmark?.title ??
                        proposal.title ??
                        "网站"}
                    </h2>
                    <span class="tag">{stateLabel(proposal.status)}</span>
                    {proposal.manual && <span class="tag">未进行 AI 分类</span>}
                    {proposal.capture_failed && <span class="status-bad">抓取失败</span>}
                  </div>
                  <a
                    href={safeUrl(
                      proposal.bookmark_url ??
                        proposal.bookmark?.url ??
                        proposal.url ??
                        "",
                    )}
                    target="_blank"
                    rel="noopener noreferrer"
                    class={s.url}
                  >
                    {proposal.bookmark_url ??
                      proposal.bookmark?.url ??
                      proposal.url}
                  </a>
                  <div class={s.paths}>
                    <div>
                      <small>当前文件夹</small>
                      <span>
                        {proposal.current_folder_path ??
                          proposal.bookmark?.folder_path ??
                          folderPath(app.folders, proposal.current_folder_id)}
                      </span>
                    </div>
                    <Icon name="arrow" />
                    <div>
                      <small>建议文件夹</small>
                      <strong>{targetLabel(proposal, app.folders)}</strong>
                      {proposal.status === "pending" &&
                        !!proposal.target?.new_segments?.length && <span class={s.newFolder}>待创建</span>}
                    </div>
                  </div>
                  {!!(
                    proposal.original_folder_path ||
                    proposal.original_folder_paths?.length
                  ) && (
                    <p class={s.source}>
                      浏览器原路径：
                      {proposal.original_folder_paths?.join("；") ||
                        proposal.original_folder_path}
                    </p>
                  )}
                  <p class={s.reason}>{proposal.reason}</p>
                </div>
                {proposal.status === "pending" && (
                  <Button disabled={busy || selectingAll} onClick={() => setEdit(proposal)} icon="edit">
                    修改目标
                  </Button>
                )}
              </article>
            ))}
          </div>
          {(proposals.page?.total ?? 0) > 48 && (
            <div class={s.pagination}>
              <Button
                disabled={page === 1 || busy || selectingAll}
                onClick={() => {
                  setPage((p) => p - 1);
                }}
              >
                上一页
              </Button>
              <span>
                {page} / {Math.ceil((proposals.page?.total ?? 0) / 48)}
              </span>
              <Button
                disabled={page * 48 >= (proposals.page?.total ?? 0) || busy || selectingAll}
                onClick={() => {
                  setPage((p) => p + 1);
                }}
              >
                下一页
              </Button>
            </div>
          )}
        </>
      )}
      {edit && (
        <EditProposal
          proposal={edit}
          onClose={() => setEdit(null)}
          onDone={() => {
            setChecked((current) => { const next = new Map(current); next.delete(edit.id); return next; });
            setEdit(null);
            app.changed();
          }}
        />
      )}
    </Dialog>
  );
}
function targetLabel(
  proposal: Proposal,
  folders: ReturnType<typeof useApp>["folders"],
) {
  if (proposal.target?.existing_folder_id || proposal.target_folder_id)
    return folderPath(
      folders,
      proposal.target?.existing_folder_id ?? proposal.target_folder_id,
    );
  if (proposal.target?.new_segments)
    return `${proposal.target.parent_folder_id ? folderPath(folders, proposal.target.parent_folder_id) + " / " : ""}${proposal.target.new_segments.join(" / ")}`;
  if (proposal.target?.existing_folder_id === null) return "未分类";
  return Array.isArray(proposal.target_path)
    ? proposal.target_path.join(" / ")
    : (proposal.target_path ?? "未分类");
}
function EditProposal({
  proposal,
  onClose,
  onDone,
}: {
  proposal: Proposal;
  onClose: () => void;
  onDone: () => void;
}) {
  const app = useApp();
  const [mode, setMode] = useState<"existing" | "new">(
    proposal.target?.new_segments?.length ? "new" : "existing",
  );
  const [folder, setFolder] = useState(
    proposal.target?.existing_folder_id ?? proposal.target_folder_id ?? "",
  );
  const [parent, setParent] = useState(proposal.target?.parent_folder_id ?? "");
  const [path, setPath] = useState(
    proposal.target?.new_segments?.join(" / ") ??
      (Array.isArray(proposal.target_path)
        ? proposal.target_path.join(" / ")
        : (proposal.target_path ?? "")),
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function save(e: Event) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await request(`/classification/proposals/${proposal.id}`, {
        method: "PATCH",
        body: {
          revision: proposal.revision,
          target:
            mode === "existing"
              ? { existing_folder_id: folder || null }
              : {
                  parent_folder_id: parent || null,
                  new_segments: path
                    .split("/")
                    .map((v) => v.trim())
                    .filter(Boolean),
                },
        },
      });
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="修改分类目标" onClose={onClose}>
      <form onSubmit={save}>
        <ErrorNotice message={error} />
        <div class={s.modes}>
          <label>
            <input
              type="radio"
              name="mode"
              checked={mode === "existing"}
              onChange={() => setMode("existing")}
            />
            已有文件夹
          </label>
          <label>
            <input
              type="radio"
              name="mode"
              checked={mode === "new"}
              onChange={() => setMode("new")}
            />
            新建路径
          </label>
        </div>
        {mode === "existing" ? (
          <Field label="目标文件夹">
            <select
              value={folder}
              onChange={(e) => setFolder(e.currentTarget.value)}
            >
              <option value="">未分类</option>
              {app.folders.map((f) => (
                <option key={f.id} value={f.id}>
                  {folderPath(app.folders, f.id)}
                </option>
              ))}
            </select>
          </Field>
        ) : (
          <>
            <Field label="新路径的父文件夹">
              <select
                value={parent}
                onChange={(e) => setParent(e.currentTarget.value)}
              >
                <option value="">根目录</option>
                {app.folders.map((f) => (
                  <option key={f.id} value={f.id}>
                    {folderPath(app.folders, f.id)}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="新建文件夹路径"
              hint="用 / 分隔多级文件夹；确认分类后才会创建。"
            >
              <input
                required
                value={path}
                placeholder="设计 / 字体 / 中文"
                onInput={(e) => setPath(e.currentTarget.value)}
              />
            </Field>
          </>
        )}
        <div class="form-actions">
          <Button onClick={onClose}>取消</Button>
          <Button primary type="submit" disabled={busy}>
            保存建议
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
