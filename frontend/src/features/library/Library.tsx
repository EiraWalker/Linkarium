import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { createPortal } from "preact/compat";
import {
  ApiError,
  errorMessage,
  idempotencyKey,
  queryString,
  request,
  safeUrl,
} from "../../api/client";
import type { Bookmark, Entity, Snapshot, Variant } from "../../api/types";
import { useApp, folderPath } from "../../app/context";
import { useMedia, useResource } from "../../app/hooks";
import {
  Button,
  Dialog,
  Empty,
  ErrorNotice,
  Field,
  Icon,
  Time,
  stateLabel,
  lockBodyScroll,
  errorCodeLabel,
} from "../../components/UI";
import { Preview, PreviewLayer, usePreviewDocuments } from "../previews/Preview";
import s from "./Library.module.css";

const searchFields = [
  { id: "title", name: "网页名" },
  { id: "domain", name: "域名" },
  { id: "url", name: "网址" },
  { id: "tags", name: "标签" },
  { id: "folder", name: "文件夹" },
  { id: "notes", name: "备注" },
];
function captureLabel(bookmark: Bookmark) {
  if (!bookmark.snapshot_enabled) return "快照已关闭";
  const state = bookmark.capture_status?.state;
  if (state === "queued") return "等待生成快照";
  if (state === "running") return "正在生成快照";
  if (state === "retry_wait") return "等待重试";
  if (state === "failed" || bookmark.capture_status?.last_error_code)
    return "快照生成失败";
  return "尚无快照";
}
function snapshotArchiveUrl(path?: string) {
  if (!path) return undefined;
  try {
    const url = new URL(path, location.origin);
    return url.origin === location.origin && url.pathname.startsWith("/api/v1/snapshots/")
      ? url.href : undefined;
  } catch { return undefined; }
}
function snapshotWarnings(snapshot?: Snapshot | null) {
  const labels = new Set<string>();
  for (const warning of snapshot?.content_warnings ??
    snapshot?.warnings ??
    []) {
    if (warning.startsWith("development-")) continue;
    if (
      warning.startsWith("missing-resource:") ||
      warning === "unreadable-resource-response"
    )
      labels.add("部分图片、字体等资源未保存，页面可能不完整。");
    else if (
      warning.startsWith("missing-css-import:") ||
      warning.startsWith("missing-stylesheet:")
    )
      labels.add("部分样式未保存，页面外观可能存在差异。");
    else if (warning.startsWith("blocked-request:"))
      labels.add("部分页面资源被安全策略阻止。");
    else if (warning === "unavailable-frame")
      labels.add("部分嵌入内容未保存。");
    else if (warning === "css-import-layer-or-supports-flattened")
      labels.add("部分样式经过兼容转换，可能存在细微差异。");
    else labels.add("部分页面内容可能存在兼容差异。");
  }
  return [...labels];
}
/** Only the scroll roots survive navigation; list content and forms still unmount. */
export function LibraryWorkspace({ active }: { active: boolean }) {
  const app = useApp();
  const [viewport, setViewport] = useState<HTMLDivElement | null>(null);
  const params = app.route.url.searchParams;
  const variant = (params.get("device") ??
    (matchMedia("(max-width:767px)").matches ? "mobile" : "desktop")) as Variant;
  return <>
    <main class={s.library} hidden={!active} data-preview-root="">
      <div ref={setViewport} />
      <PreviewLayer />
    </main>
    {active && viewport && <Library viewport={viewport} />}
    <Details
      active={active}
      id={active ? params.get("selected") : null}
      variant={variant}
      onClose={() => app.route.params({ selected: undefined, panel: undefined })}
    />
  </>;
}

export function Library({ viewport }: { viewport: HTMLDivElement }) {
  const app = useApp();
  const { url, params } = app.route;
  const p = url.searchParams;
  const compact = useMedia("(max-width:1199px)");
  const folder = p.get("folder");
  const q = p.get("q") ?? "";
  const fields = p.get("fields") ?? searchFields.map((f) => f.id).join(",");
  const variant = (p.get("device") ??
    (matchMedia("(max-width:767px)").matches
      ? "mobile"
      : "desktop")) as Variant;
  const page = Number(p.get("page") ?? 1);
  const selected = p.get("selected");
  const scope = p.get("scope") ?? (q ? "all" : folder ? "folder" : "all");
  const filters = {
    q,
    fields,
    scope,
    folder_id: folder ?? undefined,
    include_descendants: true,
    tag_ids: p.get("tags"),
    starred: p.get("starred") === "true" ? true : undefined,
    unclassified: p.get("unclassified") === "true" ? true : undefined,
  };
  const selectionQuery = queryString(filters);
  const query = queryString({
    ...filters,
    variant,
    sort: p.get("sort") ?? "added_at",
    order: p.get("order") ?? "desc",
    page,
    page_size: 48,
  });
  const resource = useResource<Bookmark[]>(`/bookmarks?${query}`, app.refresh);
  useLayoutEffect(() => {
    viewport.parentElement?.setAttribute("aria-busy", String(resource.loading));
  }, [viewport, resource.loading]);
  const [input, setInput] = useState(q);
  const [showFields, setShowFields] = useState(false);
  const [adding, setAdding] = useState(false);
  const [action, setAction] = useState<
    "move" | "add_tags" | "remove_tags" | "delete" | null
  >(null);
  const [checked, setChecked] = useState<Map<string, Entity>>(new Map());
  const [selectingAll, setSelectingAll] = useState(false);
  const selectRequest = useRef<AbortController | null>(null);
  const selectionScope = `${app.libraryIdentity}:${selectionQuery}`;
  const currentScope = useRef(selectionScope);
  currentScope.current = selectionScope;
  const selectAllRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const fieldsRef = useRef<HTMLDivElement>(null);
  const starBusy = useRef(new Set<string>());
  const title = folder
    ? folderPath(app.folders, folder)
    : p.get("unclassified")
      ? "未分类"
      : p.get("starred")
        ? "星标收藏"
        : "全部网站";
  const bookmarks = resource.data ?? [];
  const total = resource.page?.total ?? bookmarks.length;
  const allSites = !folder && !p.get("unclassified") && !p.get("starred");
  const actionFolder = folder ?? (allSites ? "all" : null);
  useEffect(() => {
    setInput(q);
  }, [q]);
  useEffect(() => {
    const timer = setTimeout(() => {
      if (input !== q)
        params(
          {
            q: input || undefined,
            page: undefined,
            scope: input ? scope : undefined,
          },
          true,
        );
    }, 250);
    return () => clearTimeout(timer);
  }, [input, q]);
  useEffect(() => {
    selectRequest.current?.abort();
    setSelectingAll(false);
    setChecked(new Map());
    return () => selectRequest.current?.abort();
  }, [selectionScope]);
  useEffect(() => {
    if (selectAllRef.current)
      selectAllRef.current.indeterminate = checked.size > 0 && checked.size < total;
  }, [checked.size, total, resource.data]);
  async function selectAll() {
    if (checked.size === total && total > 0) {
      setChecked(new Map());
      return;
    }
    selectRequest.current?.abort();
    const controller = new AbortController();
    selectRequest.current = controller;
    setSelectingAll(true);
    setError("");
    try {
      const { data } = await request<Entity[]>(`/bookmarks?${selectionQuery}&selection=true`, { signal: controller.signal });
      if (!controller.signal.aborted && currentScope.current === selectionScope) {
        setChecked(new Map(data.map((item) => [item.id, item])));
        resource.reload();
      }
    } catch (e) {
      if (!controller.signal.aborted && currentScope.current === selectionScope) setError(errorMessage(e));
    } finally {
      if (!controller.signal.aborted && currentScope.current === selectionScope) setSelectingAll(false);
    }
  }
  useEffect(() => {
    if (!showFields) return;
    const close = (e: PointerEvent) => {
      if (!fieldsRef.current?.contains(e.target as Node)) setShowFields(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") setShowFields(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", key);
    };
  }, [showFields]);
  async function star(bookmark: Bookmark) {
    if (starBusy.current.has(bookmark.id)) return;
    starBusy.current.add(bookmark.id);
    setError("");
    try {
      await request(`/bookmarks/${bookmark.id}`, {
        method: "PATCH",
        body: { revision: bookmark.revision, is_starred: !bookmark.is_starred },
      });
      app.changed();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      starBusy.current.delete(bookmark.id);
    }
  }
  async function refreshFolder() {
    setBusy("refresh");
    setError("");
    try {
      if (!actionFolder) {
        app.notify("请先选择要刷新的文件夹。");
        return;
      }
      const { data } = await request<{
        eligible: number;
        disabled: number;
        active: number;
      }>(`/folders/${actionFolder}/refresh-preview`, { method: "POST", body: {} });
      setRefreshPreview(data);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }
  const [refreshPreview, setRefreshPreview] = useState<{
    eligible: number;
    disabled: number;
    active: number;
  } | null>(null);
  async function startRefresh() {
    setBusy("refresh");
    try {
      const { data } = await request<{ group_id: string }>(
        `/folders/${actionFolder}/refresh`,
        { method: "POST", body: {}, key: idempotencyKey() },
      );
      app.trackGroup(data.group_id);
      setRefreshPreview(null);
      app.notify("已加入后台刷新队列。");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }
  function toggle(bookmark: Bookmark) {
    setChecked((current) => {
      const next = new Map(current);
      next.has(bookmark.id) ? next.delete(bookmark.id) : next.set(bookmark.id, { id: bookmark.id, revision: bookmark.revision });
      return next;
    });
  }
  async function classifyWebsites() {
    if (busy || selectingAll) return;
    const selectedIds = [...checked.keys()];
    setBusy("classify");
    setError("");
    try {
      const body = selectedIds.length
        ? { bookmark_ids: selectedIds }
        : actionFolder
          ? { folder_id: actionFolder }
          : { bookmark_ids: (await request<Entity[]>(`/bookmarks?${selectionQuery}&selection=true`)).data.map((item) => item.id) };
      const { data } = await request<{ group_id: string }>("/classification/batches", {
        method: "POST", body, key: idempotencyKey(),
      });
      app.trackGroup(data.group_id);
      app.notify(`已提交${selectedIds.length ? `选中的 ${selectedIds.length} 个网站` : actionFolder ? allSites ? "全部网站" : "文件夹" : "当前列表"}的 AI 分类任务，可从后台任务校对分类。`);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }
  const pages = Math.max(1, Math.ceil(total / 48));
  return (
    <>
      {createPortal(<>
        <div class={s.pageHeading}>
          <div class={s.title}>
            <h1>{title}</h1>
            <span>{total} 个网站</span>
          </div>
          <div class={s.headingActions}>
            <Button
              icon="refresh"
              onClick={refreshFolder}
              disabled={!actionFolder || !!busy}
            >
              刷新文件夹
            </Button>
            {checked.size === 0 && (
              <Button disabled={total === 0 || resource.loading || selectingAll || !!busy} onClick={() => void classifyWebsites()}>AI分类</Button>
            )}
            <Button icon="plus" primary onClick={() => setAdding(true)}>
              添加网站
            </Button>
          </div>
        </div>
        <div class={s.searchRow}>
          <div class={s.searchMode} ref={fieldsRef}>
            <Button onClick={() => setShowFields((v) => !v)}>
              搜索方式 <span class={s.down}>⌄</span>
            </Button>
            {showFields && (
              <div
                class={s.searchPopover}
                role="group"
                aria-label="参与搜索的字段"
              >
                {searchFields.map((field) => (
                  <label key={field.id}>
                    <input
                      type="checkbox"
                      checked={fields.split(",").includes(field.id)}
                      onChange={() => {
                        const set = new Set(fields.split(","));
                        set.has(field.id)
                          ? set.delete(field.id)
                          : set.add(field.id);
                        if (set.size)
                          params(
                            { fields: [...set].join(","), page: undefined },
                            true,
                          );
                      }}
                    />
                    {field.name}
                  </label>
                ))}
                <small>至少保留一个搜索字段</small>
              </div>
            )}
          </div>
          <div class={s.searchInput}>
            <Icon name="search" />
            <input
              aria-label="搜索网站"
              type="search"
              placeholder="搜索网站、标签、文件夹、备注"
              value={input}
              onInput={(e) => setInput(e.currentTarget.value)}
            />
          </div>
        </div>
        <div class={s.filters}>
          <div class={s.filterLeft}>
            <select
              aria-label="搜索范围"
              value={scope}
              onChange={(e) =>
                params({ scope: e.currentTarget.value, page: undefined })
              }
            >
              <option value="all">全库搜索</option>
              <option value="folder" disabled={!folder}>
                当前文件夹及子文件夹
              </option>
            </select>
            <details class={s.tagFilter}>
              <summary>
                标签
                {p.get("tags") ? ` · ${p.get("tags")!.split(",").length}` : ""}
              </summary>
              <div>
                {app.tags.length ? (
                  app.tags.map((tag) => (
                    <label key={tag.id}>
                      <input
                        type="checkbox"
                        checked={(p.get("tags") ?? "")
                          .split(",")
                          .includes(tag.id)}
                        onChange={() => {
                          const ids = new Set(
                            (p.get("tags") ?? "").split(",").filter(Boolean),
                          );
                          ids.has(tag.id)
                            ? ids.delete(tag.id)
                            : ids.add(tag.id);
                          params({
                            tags: [...ids].join(",") || undefined,
                            page: undefined,
                          });
                        }}
                      />
                      {tag.name}
                    </label>
                  ))
                ) : (
                  <small>暂无标签</small>
                )}
              </div>
            </details>
            <select
              aria-label="排序方式"
              value={p.get("sort") ?? "added_at"}
              onChange={(e) =>
                params({ sort: e.currentTarget.value, page: undefined })
              }
            >
              <option value="added_at">最近添加</option>
              <option value="snapshot_updated_at">快照更新时间</option>
              <option value="title">网页名</option>
            </select>
          </div>
          <div class={s.deviceButtons}>
            <button
              aria-pressed={variant === "desktop"}
              onClick={() => params({ device: "desktop", page: undefined })}
              title="桌面快照"
            >
              <Icon name="desktop" />
            </button>
            <button
              aria-pressed={variant === "mobile"}
              onClick={() => params({ device: "mobile", page: undefined })}
              title="手机快照"
            >
              <Icon name="mobile" />
            </button>
          </div>
        </div>
        <ErrorNotice
          message={error || resource.error}
          retry={resource.error ? resource.reload : undefined}
        />
        {bookmarks.length > 0 && (
          <div class={s.selectionRow}>
            <label>
              <input
                type="checkbox"
                ref={selectAllRef}
                aria-label="全选"
                checked={total > 0 && checked.size === total}
                disabled={selectingAll || resource.loading}
                onChange={() => void selectAll()}
              />{" "}
              全选
            </label>
            {selectingAll && <span role="status">正在选择所有页…</span>}
            {checked.size > 0 && <span>已选择 {checked.size} 项</span>}
            {checked.size > 0 && (
              <>
                <Button disabled={selectingAll || !!busy} onClick={() => void classifyWebsites()}>AI分类</Button>
                <Button disabled={selectingAll} onClick={() => setAction("move")}>移动</Button>
                <Button disabled={selectingAll} onClick={() => setAction("add_tags")}>添加标签</Button>
                <Button disabled={selectingAll} onClick={() => setAction("remove_tags")}>
                  移除标签
                </Button>
                <Button disabled={selectingAll} danger onClick={() => setAction("delete")} icon="trash">
                  删除
                </Button>
                <Button disabled={selectingAll} onClick={() => setChecked(new Map())}>取消选择</Button>
              </>
            )}
          </div>
        )}
        {!resource.data && resource.loading ? (
          <div class={s.grid}>
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} class={s.skeleton} />
            ))}
          </div>
        ) : !bookmarks.length ? (
          <Empty title={q ? "没有匹配的网站" : "这里还没有网站"}>
            <p>
              {q
                ? "试试其他关键词、字段或搜索范围。"
                : "添加一个网站，或导入浏览器书签开始整理。"}
            </p>
            <Button icon="plus" onClick={() => setAdding(true)}>
              添加网站
            </Button>
          </Empty>
        ) : (
          <div class={s.grid}>
            {bookmarks.map((bookmark) => (
              <article
                key={bookmark.id}
                class={`${s.card} ${selected === bookmark.id ? s.selectedCard : ""}`}
              >
                <div class={s.cardPreview}>
                  <Preview
                      snapshot={bookmark.preview}
                      bookmarkId={bookmark.id}
                    fallback={captureLabel(bookmark)}
                    disabled={!!selected && compact}
                    title={bookmark.title}
                  />
                  <button
                    class={s.previewSelect}
                    onClick={() => params({ selected: bookmark.id })}
                    aria-label={`查看 ${bookmark.title}`}
                    aria-pressed={selected === bookmark.id}
                  />
                  <label class={s.cardCheckbox} title="选择网站">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${bookmark.title}`}
                      checked={checked.has(bookmark.id)}
                      disabled={selectingAll}
                      onChange={() => toggle(bookmark)}
                    />
                  </label>
                </div>
                <div class={s.cardBody}>
                  <div class={s.cardTitleRow}>
                    <button
                      class={s.cardTitle}
                      onClick={() => params({ selected: bookmark.id })}
                    >
                      {bookmark.title || bookmark.domain}
                    </button>
                    <button
                      class={`${s.star} ${bookmark.is_starred ? s.starred : ""}`}
                      aria-label={`${bookmark.is_starred ? "取消" : "添加"}星标 ${bookmark.title}`}
                      aria-pressed={bookmark.is_starred}
                      onClick={() => void star(bookmark)}
                    >
                      <Icon name="star" size={16} />
                    </button>
                    <a
                      href={safeUrl(bookmark.url)}
                      target="_blank"
                      rel="noopener noreferrer"
                      class={s.openLink}
                      aria-label={`打开原网站 ${bookmark.title}`}
                    >
                      <Icon name="external" size={15} />
                    </a>
                  </div>
                  <span class={s.domain}>{bookmark.domain}</span>
                  <div class={s.cardTags}>
                    {bookmark.tags.slice(0, 3).map((tag) => (
                      <span class="tag" key={tag.id}>
                        {tag.name}
                      </span>
                    ))}
                    {bookmark.tags.length > 3 && (
                      <span class="tag">+{bookmark.tags.length - 3}</span>
                    )}
                  </div>
                  <small
                    class={s.cardTime}
                    title={
                      bookmark.capture_status?.last_error_code
                        ? errorCodeLabel(
                            bookmark.capture_status.last_error_code,
                          )
                        : undefined
                    }
                  >
                    {bookmark.preview ? (
                      <>
                        <Time value={bookmark.preview.captured_at} />
                        {bookmark.capture_status?.last_error_code && (
                          <span class="status-bad"> · 最近刷新失败</span>
                        )}
                      </>
                    ) : !bookmark.snapshot_enabled ? (
                      "快照已关闭"
                    ) : bookmark.auto_refresh_paused ? (
                      "自动刷新已暂停"
                    ) : (
                      captureLabel(bookmark)
                    )}
                  </small>
                </div>
              </article>
            ))}
          </div>
        )}
        {pages > 1 && (
          <nav class={s.pagination} aria-label="网站分页">
            <Button
              disabled={page <= 1}
              onClick={() =>
                params({ page: String(page - 1), selected: undefined })
              }
            >
              上一页
            </Button>
            <span>
              {page} / {pages}
            </span>
            <Button
              disabled={page >= pages}
              onClick={() =>
                params({ page: String(page + 1), selected: undefined })
              }
            >
              下一页
            </Button>
          </nav>
        )}
      </>, viewport)}
      {adding && (
        <AddBookmark onClose={() => setAdding(false)} folderId={folder} />
      )}
      {action && (
        <BulkDialog
          action={action}
          bookmarks={[...checked.values()]}
          onClose={() => setAction(null)}
          onDone={() => {
            setAction(null);
            setChecked(new Map());
            app.changed();
          }}
        />
      )}
      {refreshPreview && (
        <Dialog title="刷新文件夹" onClose={() => setRefreshPreview(null)}>
          <p>
            将刷新{allSites ? "全部网站中的" : "此文件夹及所有子文件夹下的"} {refreshPreview.eligible}{" "}
            个网站，分别生成桌面和手机快照。
          </p>
          <p class="muted">
            快照关闭：{refreshPreview.disabled} · 已在队列：
            {refreshPreview.active}
          </p>
          <div class="form-actions">
            <Button onClick={() => setRefreshPreview(null)}>取消</Button>
            <Button
              primary
              onClick={() => void startRefresh()}
              disabled={!!busy}
            >
              立即刷新
            </Button>
          </div>
        </Dialog>
      )}
    </>
  );
}
function AddBookmark({
  onClose,
  folderId,
}: {
  onClose: () => void;
  folderId: string | null;
}) {
  const app = useApp();
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [folder, setFolder] = useState(folderId ?? "");
  const [snapshot, setSnapshot] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e: Event) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const { data } = await request<
        Bookmark & { created?: boolean; bookmark?: Bookmark }
      >("/bookmarks", {
        method: "POST",
        body: {
          url,
          title,
          folder_id: folder || null,
          snapshot_enabled: snapshot,
        },
        key: idempotencyKey(),
      });
      const b = data.bookmark ?? data;
      app.changed();
      app.route.params({ selected: b.id });
      app.notify(
        data.created === false
          ? "网站已经存在，已定位现有记录。"
          : snapshot
            ? "已添加网站，快照将在后台生成。"
            : "已添加网站，快照保持关闭。",
      );
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="添加网站" onClose={onClose}>
      <form onSubmit={submit}>
        <ErrorNotice message={error} />
        <Field label="网址">
          <input
            type="url"
            required
            autoFocus
            placeholder="https://example.com"
            value={url}
            onInput={(e) => setUrl(e.currentTarget.value)}
          />
        </Field>
        <Field label="网页名" hint="留空时由后台读取页面标题。">
          <input
            value={title}
            maxLength={500}
            onInput={(e) => setTitle(e.currentTarget.value)}
          />
        </Field>
        <Field label="文件夹">
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
        <label class="inline">
          <input
            type="checkbox"
            checked={snapshot}
            onChange={(e) => setSnapshot(e.currentTarget.checked)}
          />
          保存桌面和手机快照
        </label>
        <div class="form-actions">
          <Button onClick={onClose}>取消</Button>
          <Button primary type="submit" disabled={busy}>
            {busy ? "正在添加…" : "添加网站"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
function BulkDialog({
  action,
  bookmarks,
  onClose,
  onDone,
}: {
  action: "move" | "add_tags" | "remove_tags" | "delete";
  bookmarks: Entity[];
  onClose: () => void;
  onDone: () => void;
}) {
  const app = useApp();
  const previews = usePreviewDocuments();
  const [folder, setFolder] = useState("");
  const [tags, setTags] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (action === "delete")
      request<Record<string, unknown>>("/bookmarks/delete-preview", {
        method: "POST",
        body: { ids: bookmarks.map((b) => b.id) },
      })
        .then((r) => setPreview(r.data))
        .catch((e) => setError(errorMessage(e)));
  }, [action]);
  async function submit() {
    setBusy(true);
    setError("");
    try {
      await request("/bookmarks/bulk", {
        method: "POST",
        body: {
          action,
          items: bookmarks.map((b) => ({ id: b.id, revision: b.revision })),
          ...(action === "move"
            ? { folder_id: folder || null }
            : action === "delete"
              ? { preview_token: preview?.token ?? preview?.preview_token }
              : { tag_ids: [...tags] }),
        },
        key: idempotencyKey(),
      });
      if (action === "delete") previews.forgetBookmarks(bookmarks.map((b) => b.id));
      app.notify(`已处理 ${bookmarks.length} 个网站。`);
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      title={
        {
          move: "移动网站",
          add_tags: "添加标签",
          remove_tags: "移除标签",
          delete: "删除网站",
        }[action]
      }
      onClose={onClose}
    >
      <ErrorNotice message={error} />
      <p>已选择 {bookmarks.length} 个网站。</p>
      {action === "move" ? (
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
      ) : action === "delete" ? (
        <>
          <p>删除网站也会删除其所有桌面、手机快照历史。</p>
          {preview ? (
            <p class="muted">
              {String(preview.snapshot_count ?? preview.versions ?? 0)}{" "}
              份快照将删除。此操作无法撤销。
            </p>
          ) : (
            <p class="muted">正在计算影响范围…</p>
          )}
        </>
      ) : (
        <div class={s.tagChoices}>
          {app.tags.map((tag) => (
            <label key={tag.id}>
              <input
                type="checkbox"
                checked={tags.has(tag.id)}
                onChange={() =>
                  setTags((old) => {
                    const next = new Set(old);
                    next.has(tag.id) ? next.delete(tag.id) : next.add(tag.id);
                    return next;
                  })
                }
              />
              {tag.name}
            </label>
          ))}
        </div>
      )}
      <div class="form-actions">
        <Button onClick={onClose}>取消</Button>
        <Button
          primary={action !== "delete"}
          danger={action === "delete"}
          disabled={
            busy ||
            (action === "delete" && !preview) ||
            ((action === "add_tags" || action === "remove_tags") && !tags.size)
          }
          onClick={() => void submit()}
        >
          {busy ? "正在处理…" : "确认"}
        </Button>
      </div>
    </Dialog>
  );
}
function Details({
  active,
  id,
  variant,
  onClose,
}: {
  active: boolean;
  id: string | null;
  variant: Variant;
  onClose: () => void;
}) {
  const app = useApp();
  const mobile = useMedia("(max-width:1199px)");
  const [device, setDevice] = useState(variant);
  const [history, setHistory] = useState(false);
  const [historyPage, setHistoryPage] = useState(1);
  const [chosen, setChosen] = useState<Snapshot | null>(null);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const resource = useResource<Bookmark>(
    id ? `/bookmarks/${id}?variant=${device}` : null,
    app.refresh,
  );
  const versions = useResource<Snapshot[]>(
    id
      ? `/bookmarks/${id}/snapshots?variant=${device}&page_size=48&page=${historyPage}`
      : null,
    app.refresh,
  );
  const bookmark = resource.data;
  const panelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    setDevice(variant);
    setChosen(null);
    setHistory(false);
    setHistoryPage(1);
    setExpanded(false);
    setEditing(false);
  }, [id, variant]);
  useEffect(() => {
    setChosen(null);
    setHistoryPage(1);
  }, [device]);
  useEffect(() => {
    if (!id || !mobile) return;
    const prior = document.activeElement as HTMLElement | null;
    const unlock = lockBodyScroll();
    panelRef.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab") {
        const focusable = panelRef.current?.querySelectorAll<HTMLElement>(
          'button,a[href],input,select,textarea,[tabindex="0"]',
        );
        if (!focusable?.length) return;
        const first = focusable[0],
          last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => {
      unlock();
      document.removeEventListener("keydown", key);
      prior?.focus();
    };
  }, [id, mobile]);
  async function refresh() {
    if (!bookmark) return;
    setBusy(true);
    setError("");
    try {
      const { data } = await request<{
        group_id: string;
        state: string;
        reason?: string;
      }>(`/bookmarks/${bookmark.id}/refresh`, {
        method: "POST",
        body: {},
        key: idempotencyKey(),
      });
      if (data.group_id) app.trackGroup(data.group_id);
      app.notify(
        data.state === "skipped"
          ? "快照已关闭，已跳过。"
          : "已加入后台刷新队列。",
      );
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  const shown =
    chosen ??
    versions.data?.find((v) => v.is_current) ??
    bookmark?.preview ??
    versions.data?.[0];
  const warnings = snapshotWarnings(shown);
  const completeSnapshotUrl = snapshotArchiveUrl(shown?.preview_url);
  return (
    <>
      {id && mobile && <div class={s.detailBackdrop} onClick={onClose} />}
      <div
        ref={panelRef}
        hidden={!active}
        data-preview-root=""
        class={`${s.details} ${id ? s.hasDetail : ""} ${expanded ? s.expanded : ""}`}
        tabIndex={-1}
        role={id && mobile ? "dialog" : "complementary"}
        aria-modal={id && mobile ? true : undefined}
        aria-label="网站详情"
      >
        <PreviewLayer />
        {!id ? (
          <Empty title="选择一个网站">
            <p>查看快照、标签和备注</p>
          </Empty>
        ) : (
          <>
            <div class={s.detailHeading}>
              <div>
                <h2>{bookmark?.title ?? "网站详情"}</h2>
                <small>{bookmark?.domain}</small>
              </div>
              <Button icon="close" title="关闭详情" onClick={onClose} />
            </div>
            {mobile && (
              <Button onClick={() => setExpanded((v) => !v)}>
                {expanded ? "收起详情" : "展开详情"}
              </Button>
            )}
            <ErrorNotice
              message={error || resource.error}
              retry={resource.error ? resource.reload : undefined}
            />
            {bookmark && (
              <>
                <div class={s.variantTabs}>
                  <button
                    aria-pressed={device === "desktop"}
                    onClick={() => setDevice("desktop")}
                  >
                    桌面
                  </button>
                  <button
                    aria-pressed={device === "mobile"}
                    onClick={() => setDevice("mobile")}
                  >
                    手机
                  </button>
                </div>
                <Preview
                    snapshot={shown}
                    bookmarkId={bookmark.id}
                  detail
                  title={`${bookmark.title} ${device === "desktop" ? "桌面" : "手机"}快照`}
                  fallback={
                    !bookmark.snapshot_enabled
                      ? "快照关闭，历史仍保留"
                      : "尚无此设备快照"
                  }
                />
                {shown && (
                  <small class={s.previewTime}>
                    <Time value={shown.captured_at} />
                    {chosen && !chosen.is_current ? " · 历史版本" : ""}
                  </small>
                )}
                {!!warnings.length && (
                  <details class={s.contentWarnings}>
                    <summary>快照提示（{warnings.length}）</summary>
                    <ul>
                      {warnings.map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  </details>
                )}
                <a
                  class={s.openOriginal}
                  href={safeUrl(bookmark.url)}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  打开原网站 <Icon name="external" size={16} />
                </a>
                {completeSnapshotUrl && (
                  <a
                    class={s.openOriginal}
                    href={completeSnapshotUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    查看完整快照 <Icon name="external" size={16} />
                  </a>
                )}
                <div class={s.detailActions}>
                  <Button icon="edit" onClick={() => setEditing(true)}>
                    编辑
                  </Button>
                  <Button
                    onClick={() => {
                      void request<{ group_id: string }>(
                        "/classification/batches",
                        {
                          method: "POST",
                          body: { bookmark_ids: [bookmark.id] },
                          key: idempotencyKey(),
                        },
                      )
                        .then(({ data }) => {
                          app.trackGroup(data.group_id);
                          app.notify("已提交分类任务，建议需人工确认。");
                        })
                        .catch((e) => setError(errorMessage(e)));
                    }}
                  >
                    AI 分类
                  </Button>
                  <Button onClick={() => app.review({ bookmarkId: bookmark.id })}>查看建议</Button>
                  <Button
                    icon="refresh"
                    onClick={() => void refresh()}
                    disabled={busy || !bookmark.snapshot_enabled}
                  >
                    立即刷新
                  </Button>
                </div>
                <dl class={s.metadata}>
                  <dt>文件夹</dt>
                  <dd>
                    {bookmark.folder_path ||
                      folderPath(app.folders, bookmark.folder_id)}
                  </dd>
                  <dt>标签</dt>
                  <dd class="inline">
                    {bookmark.tags.length ? (
                      bookmark.tags.map((t) => (
                        <span class="tag" key={t.id}>
                          {t.name}
                        </span>
                      ))
                    ) : (
                      <span class="muted">无标签</span>
                    )}
                  </dd>
                  <dt>备注</dt>
                  <dd class={s.notes}>{bookmark.notes || "暂无备注"}</dd>
                  <dt>刷新间隔</dt>
                  <dd>
                    {bookmark.refresh_interval_seconds
                      ? intervalLabel(bookmark.refresh_interval_seconds)
                      : "跟随默认（" +
                        intervalLabel(
                          app.settings?.default_refresh_seconds ?? 604800,
                        ) +
                        "）"}
                  </dd>
                  <dt>保存快照</dt>
                  <dd>
                    {bookmark.snapshot_enabled ? "已开启" : "已关闭 · 历史保留"}
                  </dd>
                  <dt>自动刷新</dt>
                  <dd>
                    {bookmark.auto_refresh_paused ? "已暂停" : "按间隔刷新"}
                  </dd>
                </dl>
                {bookmark.capture_states?.map((c) => (
                  <div class={s.captureStatus} key={c.variant}>
                    <strong>{c.variant === "desktop" ? "桌面" : "手机"}</strong>
                    <span>
                      {c.last_error_code
                        ? c.current_version_id || c.current
                          ? "最近刷新失败 · 旧快照保留"
                          : "最近刷新失败 · 尚无成功快照"
                        : c.state
                          ? stateLabel(c.state)
                          : c.last_success_at
                            ? "已有成功快照"
                            : "等待快照"}
                    </span>
                    {c.last_error_code && (
                      <small class="status-bad">
                        {errorCodeLabel(c.last_error_code)}
                      </small>
                    )}
                    <small>
                      最后成功 <Time value={c.last_success_at} />
                    </small>
                  </div>
                ))}
                <div class={s.historyHeading}>
                  <h3>快照历史</h3>
                  <button
                    onClick={() => {
                      setHistory((v) => !v);
                      setHistoryPage(1);
                    }}
                  >
                    {history ? "收起" : "查看全部"}
                  </button>
                </div>
                <ErrorNotice message={versions.error} />
                {versions.data?.length ? (
                  <ul class={s.history}>
                    {versions.data.slice(0, history ? 48 : 3).map((v) => (
                      <li key={v.id ?? v.version_id}>
                        <button
                          onClick={() => setChosen(v)}
                          class={
                            chosen?.version_id === v.version_id
                              ? s.historyChosen
                              : ""
                          }
                        >
                          <span class={s.historyDot} />
                          <Time value={v.captured_at} />
                          {v.is_current && <span class={s.latest}>最新</span>}
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p class="muted">尚无此设备历史</p>
                )}
                {history && (versions.page?.total ?? 0) > 48 && (
                  <nav class="inline" aria-label="快照历史分页">
                    <Button
                      disabled={historyPage <= 1}
                      onClick={() => setHistoryPage((value) => value - 1)}
                    >
                      上一页
                    </Button>
                    <small>
                      {historyPage} /{" "}
                      {Math.ceil((versions.page?.total ?? 0) / 48)}
                    </small>
                    <Button
                      disabled={historyPage * 48 >= (versions.page?.total ?? 0)}
                      onClick={() => setHistoryPage((value) => value + 1)}
                    >
                      下一页
                    </Button>
                  </nav>
                )}
                {history && !!versions.data?.length && (
                  <HistoryCleanup
                    versions={versions.data}
                    onDone={() => {
                      setChosen(null);
                      app.changed();
                    }}
                  />
                )}
              </>
            )}
            {resource.loading && !bookmark && <p class="muted">正在加载…</p>}
          </>
        )}
      </div>
      {editing && bookmark && (
        <EditBookmark bookmark={bookmark} onClose={() => setEditing(false)} />
      )}
    </>
  );
}
function intervalLabel(value: number) {
  return value % 604800 === 0
    ? `每 ${value / 604800} 周`
    : value % 86400 === 0
      ? `每 ${value / 86400} 天`
      : value % 3600 === 0
        ? `每 ${value / 3600} 小时`
        : `每 ${Math.round(value / 60)} 分钟`;
}
function EditBookmark({
  bookmark,
  onClose,
}: {
  bookmark: Bookmark;
  onClose: () => void;
}) {
  const app = useApp();
  const [title, setTitle] = useState(bookmark.title);
  const [url, setUrl] = useState(bookmark.url);
  const [notes, setNotes] = useState(bookmark.notes ?? "");
  const [folder, setFolder] = useState(bookmark.folder_id ?? "");
  const [tags, setTags] = useState<Set<string>>(
    new Set(bookmark.tags.map((t) => t.id)),
  );
  const [enabled, setEnabled] = useState(bookmark.snapshot_enabled);
  const [paused, setPaused] = useState(bookmark.auto_refresh_paused ?? false);
  const [interval, setInterval] = useState(
    bookmark.refresh_interval_seconds
      ? String(bookmark.refresh_interval_seconds / 86400)
      : "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [tagName, setTagName] = useState("");
  const dirty =
    title !== bookmark.title ||
    url !== bookmark.url ||
    notes !== (bookmark.notes ?? "") ||
    folder !== (bookmark.folder_id ?? "") ||
    enabled !== bookmark.snapshot_enabled ||
    paused !== (bookmark.auto_refresh_paused ?? false) ||
    interval !==
      (bookmark.refresh_interval_seconds
        ? String(bookmark.refresh_interval_seconds / 86400)
        : "") ||
    [...tags].sort().join() !==
      bookmark.tags
        .map((t) => t.id)
        .sort()
        .join();
  function close() {
    if (!dirty || confirm("尚有未保存的修改，确定关闭？")) onClose();
  }
  async function save(e: Event) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await request(`/bookmarks/${bookmark.id}`, {
        method: "PATCH",
        body: {
          revision: bookmark.revision,
          title,
          url,
          notes,
          folder_id: folder || null,
          tag_ids: [...tags],
          snapshot_enabled: enabled,
          auto_refresh_paused: paused,
          refresh_interval_seconds: interval
            ? Math.round(Number(interval) * 86400)
            : null,
        },
      });
      app.changed();
      app.notify("已保存修改。");
      onClose();
    } catch (e) {
      setError(errorMessage(e));
      setConflict(e instanceof ApiError && e.status === 409);
    } finally {
      setBusy(false);
    }
  }
  async function addTag() {
    if (!tagName.trim()) return;
    try {
      const { data } = await request<{ id: string }>("/tags", {
        method: "POST",
        body: { name: tagName.trim() },
      });
      setTags((old) => new Set([...old, data.id]));
      setTagName("");
      app.changed();
    } catch (e) {
      setError(errorMessage(e));
    }
  }
  return (
    <Dialog title="编辑网站" onClose={close}>
      <form onSubmit={save}>
        <ErrorNotice message={error} />
        {conflict && (
          <p class="muted">
            另一台设备已经修改此网站。当前草稿已保留；请关闭并重新读取最新记录后再编辑。
          </p>
        )}
        <Field label="网页名">
          <input
            value={title}
            onInput={(e) => setTitle(e.currentTarget.value)}
            maxLength={500}
          />
        </Field>
        <Field label="网址">
          <input
            type="url"
            value={url}
            required
            onInput={(e) => setUrl(e.currentTarget.value)}
          />
        </Field>
        <Field label="文件夹">
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
        <Field label="备注">
          <textarea
            value={notes}
            onInput={(e) => setNotes(e.currentTarget.value)}
            rows={3}
          />
        </Field>
        <div class={s.tagChoices}>
          {app.tags.map((tag) => (
            <label key={tag.id}>
              <input
                type="checkbox"
                checked={tags.has(tag.id)}
                onChange={() =>
                  setTags((old) => {
                    const next = new Set(old);
                    next.has(tag.id) ? next.delete(tag.id) : next.add(tag.id);
                    return next;
                  })
                }
              />
              {tag.name}
            </label>
          ))}
        </div>
        <div class="inline">
          <input
            aria-label="新标签名称"
            placeholder="新标签"
            value={tagName}
            maxLength={100}
            onInput={(e) => setTagName(e.currentTarget.value)}
          />
          <Button onClick={() => void addTag()}>添加标签</Button>
        </div>
        <hr />
        <Field label="刷新间隔（天）" hint="留空跟随全局默认值。">
          <input
            type="number"
            step="any"
            min="0.0007"
            value={interval}
            placeholder="跟随默认"
            onInput={(e) => setInterval(e.currentTarget.value)}
          />
        </Field>
        <label class="inline">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.currentTarget.checked)}
          />
          保存快照（关闭后保留历史）
        </label>
        <label class="inline">
          <input
            type="checkbox"
            checked={paused}
            onChange={(e) => setPaused(e.currentTarget.checked)}
          />
          暂停自动刷新（仍可手动刷新）
        </label>
        <div class="form-actions">
          <Button onClick={close}>取消</Button>
          <Button primary type="submit" disabled={busy || conflict}>
            {busy ? "正在保存…" : "保存"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
function HistoryCleanup({
  versions,
  onDone,
}: {
  versions: Snapshot[];
  onDone: () => void;
}) {
  const previews = usePreviewDocuments();
  const [show, setShow] = useState(false);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function review() {
    setBusy(true);
    setError("");
    try {
      const { data } = await request<Record<string, unknown>>(
        "/snapshots/delete-preview",
        { method: "POST", body: { ids: [...chosen] } },
      );
      setPreview(data);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    setBusy(true);
    try {
      await request("/snapshots/bulk-delete", {
        method: "POST",
        body: {
          ids: [...chosen],
          preview_token: preview?.token ?? preview?.preview_token,
        },
        key: idempotencyKey(),
      });
      previews.forgetVersions(chosen);
      setShow(false);
      setChosen(new Set());
      setPreview(null);
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Button onClick={() => setShow(true)} icon="trash">
        清理旧历史
      </Button>
      {show && (
        <Dialog
          title="清理旧快照历史"
          onClose={() => {
            setShow(false);
            setPreview(null);
          }}
        >
          <p>当前设备的最新成功快照受到保护。清理不会改变网页记录。</p>
          <ErrorNotice message={error} />
          <div class={s.historyChoices}>
            {versions.map((v) => (
              <label key={v.version_id}>
                <input
                  type="checkbox"
                  disabled={v.is_current || !!preview}
                  checked={chosen.has(v.version_id ?? v.id!)}
                  onChange={() =>
                    setChosen((old) => {
                      const next = new Set(old);
                      const id = v.version_id ?? v.id!;
                      next.has(id) ? next.delete(id) : next.add(id);
                      return next;
                    })
                  }
                />
                <Time value={v.captured_at} />
                {v.is_current ? " · 最新（保留）" : ""}
              </label>
            ))}
          </div>
          {preview && <p>将永久删除 {chosen.size} 个历史版本。</p>}
          <div class="form-actions">
            <Button
              onClick={() => {
                setShow(false);
                setPreview(null);
              }}
            >
              取消
            </Button>
            <Button
              danger
              disabled={!chosen.size || busy}
              onClick={() => void (preview ? remove() : review())}
            >
              {preview ? "确认删除" : "预览删除影响"}
            </Button>
          </div>
        </Dialog>
      )}
    </>
  );
}
