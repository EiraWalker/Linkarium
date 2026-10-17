import { useEffect, useRef, useState } from "preact/hooks";
import { ApiError, errorMessage, getSession, request } from "../api/client";
import type { Folder, Session, Settings, Tag, JobGroup, ImportBatch, TaskCatalog } from "../api/types";
import { AppState, flattenFolders, folderPath, type ReviewScope } from "./context";
import { useResource, useRoute } from "./hooks";
import {
  Button,
  Dialog,
  Empty,
  ErrorNotice,
  Field,
  Icon,
  stateLabel,
  terminalState,
  lockBodyScroll,
  errorCodeLabel,
} from "../components/UI";
import { LibraryWorkspace } from "../features/library/Library";
import { ClassificationReview } from "../features/classify/Classification";
import { SettingsPage } from "../features/settings/Settings";
import { clearBrowserState } from "../features/settings/DataActions";
import { ResizableSidebar } from "./ResizableSidebar";
import { Transfers } from "../features/transfers/Transfers";
import { PreviewSession, usePreviewDocuments } from "../features/previews/Preview";
import s from "./App.module.css";
import { Login } from "../features/auth/Login";
import { clearEnrollmentIntent, consumeEnrollmentIntent } from "../features/auth/passkeyClient";

export function App() {
  const route = useRoute();
  const [session, setSession] = useState<Session | null>(null);
  const [enrollingPasskey, setEnrollingPasskey] = useState(consumeEnrollmentIntent);
  const [startupError, setStartupError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [toast, setToast] = useState("");
  const [navOpen, setNavOpen] = useState(false);
  const [folderDialog, setFolderDialog] = useState<Folder | "new" | null>(null);
  const [tagDialog, setTagDialog] = useState(false);
  const [group, setGroup] = useState<string | null>(null);
  const [showProgress, setShowProgress] = useState(false);
  const [taskPage, setTaskPage] = useState(1);
  const [reviewScope, setReviewScope] = useState<ReviewScope | null>(null);
  const notified = useRef(new Set<string>());
  const observedTasks = useRef<{ library: string; signature: string } | null>(null);
  const [maintenance, setMaintenance] = useState(false);
  useEffect(() => {
    const receiveEnrollment = () => {
      const intent = consumeEnrollmentIntent();
      if (intent) setEnrollingPasskey(intent);
    };
    window.addEventListener("hashchange", receiveEnrollment);
    return () => window.removeEventListener("hashchange", receiveEnrollment);
  }, []);
  async function sessionLoad() {
    setStartupError("");
    try {
      const next = await getSession();
      setSession(next);
      setMaintenance(next.maintenance_mode ?? false);
      if (!next.authenticated && route.url.pathname !== "/login")
        route.navigate("/login", true);
      else if (
        next.authenticated && !enrollingPasskey &&
        (route.url.pathname === "/" || route.url.pathname === "/login")
      )
        route.navigate("/library", true);
    } catch (e) {
      setStartupError(errorMessage(e));
    }
  }
  useEffect(() => {
    void sessionLoad();
    const expired = () => {
      setSession(null);
      setNavOpen(false);
      route.navigate("/login", true);
      void getSession()
        .then(setSession)
        .catch((e) => setStartupError(errorMessage(e)));
    };
    const maintain = () => setMaintenance(true);
    window.addEventListener("session-expired", expired);
    window.addEventListener("service-maintenance", maintain);
    return () => {
      window.removeEventListener("session-expired", expired);
      window.removeEventListener("service-maintenance", maintain);
    };
  }, []);
  const authorized = (session?.authenticated ?? false) && !enrollingPasskey;
  useEffect(() => {
    if (!maintenance) return;
    const timer = setInterval(() => {
      void sessionLoad().catch(() => {});
    }, 2000);
    return () => clearInterval(timer);
  }, [maintenance]);
  useEffect(() => {
    if (session?.initialization_required && !maintenance) clearBrowserState();
  }, [session?.initialization_required, maintenance]);
  const libraryIdentity = `${session?.active_generation ?? ""}:${session?.library_epoch ?? ""}`;
  const taskStorageKey = `website-task-${libraryIdentity}`;
  useEffect(() => {
    notified.current.clear();
    setReviewScope(null);
    setGroup(null);
    setShowProgress(false);
    setTaskPage(1);
    if (authorized) {
      try {
        const saved = sessionStorage.getItem(taskStorageKey);
        if (saved && /^[0-9a-f-]{36}$/i.test(saved)) setGroup(saved);
      } catch {}
    }
  }, [libraryIdentity, authorized]);
  useEffect(() => {
    if (route.url.pathname === "/classification") route.navigate("/transfers", true);
  }, [route.url.pathname]);
  const foldersQuery = useResource<Folder[]>(
    authorized ? "/folders" : null,
    refresh,
  );
  const tagsQuery = useResource<Tag[]>(authorized ? "/tags" : null, refresh);
  const settingsQuery = useResource<Settings>(
    authorized ? "/settings" : null,
    refresh,
  );
  const progress = useResource<JobGroup>(
    authorized && showProgress && group ? `/job-groups/${group}?library=${encodeURIComponent(libraryIdentity)}` : null,
    refresh,
    showProgress && group ? 2000 : 0,
  );
  const tasks = useResource<TaskCatalog>(
    authorized ? `/job-groups?page=${taskPage}&page_size=20&library=${encodeURIComponent(libraryIdentity)}` : null,
    refresh,
    authorized ? 2000 : 0,
  );
  const currentTask = tasks.data?.current_group ?? null;
  const taskStateSignature = tasks.data ? JSON.stringify([
    tasks.data.completion_revision,
    tasks.data.active_count,
    currentTask?.id,
    currentTask?.state,
    currentTask?.counts?.queued,
    currentTask?.counts?.running,
    currentTask?.counts?.retry_wait,
    currentTask?.completed,
  ]) : null;
  useEffect(() => {
    if (!authorized || taskStateSignature === null) return;
    const previous = observedTasks.current;
    observedTasks.current = { library: libraryIdentity, signature: taskStateSignature };
    if (previous?.library === libraryIdentity && previous.signature !== taskStateSignature) {
      // Also follow batches started elsewhere and retries starting automatically.
      // Equal polling responses do not refresh the catalog or reset selections.
      // This durable revision also covers tasks finishing on another device.
      // Merely opening a completed history group must not refresh the library.
      changed();
    }
  }, [authorized, libraryIdentity, taskStateSignature]);
  useEffect(() => {
    if (!showProgress && currentTask?.id && currentTask.id !== group) {
      selectGroup(currentTask.id);
    } else if (!group && tasks.data?.groups.length) {
      selectGroup(tasks.data.groups[0].id!);
    }
  }, [group, currentTask?.id, showProgress, tasks.data]);
  const listedTask = tasks.data?.groups.find((item) => item.id === group);
  useEffect(() => {
    // Finished detail polling may have stopped. Another device can still
    // approve suggestions or retry a job; the durable catalog signals changes.
    if (listedTask && progress.data &&
        (listedTask.state !== progress.data.state ||
         listedTask.pending_count !== progress.data.pending_count ||
         listedTask.completed !== progress.data.completed)) progress.reload();
  }, [group, listedTask?.state, listedTask?.pending_count, listedTask?.completed]);
  const importWorkflows = useResource<ImportBatch[]>(authorized ? `/imports?library=${encodeURIComponent(libraryIdentity)}` : null, refresh, authorized ? 2000 : 0);
  const activeImport = importWorkflows.data?.find((batch) => ["queued", "running"].includes(batch.state));
  const pendingReview = importWorkflows.data?.find((batch) => batch.review_ready && !batch.review_notified && !notified.current.has(batch.id));
  useEffect(() => {
    if (route.url.pathname !== "/transfers" || reviewScope || !pendingReview) return;
    const identity = pendingReview.id;
    notified.current.add(identity);
    route.params({ import: identity }, true);
    try { sessionStorage.setItem(`website-import-${libraryIdentity}`, identity); } catch {}
    setReviewScope({ importId: identity });
    void request(`/imports/${identity}/review-notified`, { method: "POST", body: {} })
      .then(importWorkflows.reload)
      .catch((e) => notify(errorMessage(e)));
    changed();
  }, [route.url.pathname, pendingReview?.id, reviewScope]);
  const folders = flattenFolders(foldersQuery.data ?? []);
  const tags = tagsQuery.data ?? [];
  function changed() {
    setRefresh((value) => value + 1);
    setMaintenance(false);
  }
  function notify(message: string) {
    setToast(message);
  }
  function trackGroup(id: string) {
    if (id) {
      selectGroup(id);
      setShowProgress(true);
      tasks.reload();
    }
  }
  function selectGroup(id: string) {
    setGroup(id || null);
    try { sessionStorage.setItem(taskStorageKey, id); } catch {}
  }
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 5500);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    setNavOpen(false);
  }, [route.url.pathname]);
  useEffect(() => {
    if (navOpen) return lockBodyScroll();
  }, [navOpen]);
  async function logout() {
    try {
      await request("/session/logout", { method: "POST", body: {} });
      setSession(null);
      route.navigate("/login");
      setSession(await getSession());
    } catch (e) {
      notify(errorMessage(e));
    }
  }
  const page = route.url.pathname;
  const selectedFolder = route.url.searchParams.get("folder");
  const navigation = [
    { path: "/library", label: "网站库", icon: "grid" as const },
    { path: "/transfers", label: "导入与备份", icon: "archive" as const },
    { path: "/settings", label: "设置", icon: "settings" as const },
  ];
  if (!session)
    return (
      <div class={s.startup}>
        <Icon name="bookmark" size={36} />
        <h1>Linkarium</h1>
        <ErrorNotice message={startupError} retry={() => void sessionLoad()} />
        {!startupError && <p class="muted">正在连接你的收藏库…</p>}
      </div>
    );
  if (!authorized || enrollingPasskey)
    return (
        <Login
          key={enrollingPasskey && enrollingPasskey.kind !== "obsolete" ? enrollingPasskey.token : "login"}
          initializationRequired={session.initialization_required ?? false}
          passwordEnabled={session.password_login_enabled ?? false}
          maintenanceMode={maintenance}
          enrollmentIntent={enrollingPasskey}
          passkeyOrigin={session.passkey_origin}
          passkeyAvailable={session.passkey_available}
          passkeyConfigured={session.passkey_configured}
        onLogin={async (notice) => {
          clearEnrollmentIntent();
          if (enrollingPasskey) {
            setEnrollingPasskey(null);
            route.navigate("/library", true);
          }
          await sessionLoad();
          changed();
          if (notice) notify(notice);
        }}
      />
    );
  return (
    <AppState.Provider
      value={{
        folders,
        tags,
        settings: settingsQuery.data,
        refresh,
        changed,
        notify,
        route,
        trackGroup,
        review: setReviewScope,
        imports: importWorkflows.data ?? [],
        reloadImports: importWorkflows.reload,
        libraryIdentity,
      }}
    >
      <PreviewSession key={libraryIdentity}>
      <div class={s.app}>
        <header class={s.header}>
          <button
            class={s.mobileMenu}
            onClick={() => setNavOpen((v) => !v)}
            aria-label="打开导航"
          >
            <Icon name="menu" />
          </button>
          <a
            href="/library"
            onClick={(e) => {
              if (!e.ctrlKey && !e.metaKey && !e.shiftKey) {
                e.preventDefault();
                route.navigate("/library");
              }
            }}
            class={s.brand}
          >
            <Icon name="bookmark" size={24} />
            <span>Linkarium</span>
          </a>
          <nav class={s.topNav} aria-label="主页面">
            {navigation.map((n) => (
              <a
                href={n.path}
                key={n.path}
                aria-current={page === n.path ? "page" : undefined}
                onClick={(e) => {
                  if (!e.ctrlKey && !e.metaKey && !e.shiftKey) {
                    e.preventDefault();
                    route.navigate(n.path);
                  }
                }}
              >
                <Icon name={n.icon} size={16} />
                {n.label}
              </a>
            ))}
          </nav>
          <Button
            icon="logout"
            title="退出登录"
            onClick={() => void logout()}
          />
        </header>
        {maintenance && (
          <div class={s.maintenance} role="status">
            <Icon name="clock" />
            服务维护中。后台任务继续执行，完成后将重新连接。
            <Button onClick={() => void sessionLoad()}>重新连接</Button>
          </div>
        )}
        <div class={`${s.body} ${activeImport ? s.importing : ""}`}>
          {navOpen && (
            <div class={s.navBackdrop} onClick={() => setNavOpen(false)} />
          )}
            <ResizableSidebar open={navOpen}>
            <div class={s.sidebarTop}>
              <button
                class={
                  page === "/library" &&
                  !selectedFolder &&
                  !route.url.searchParams.has("starred") &&
                  !route.url.searchParams.has("unclassified")
                    ? s.navSelected
                    : ""
                }
                onClick={() => {
                  route.navigate("/library");
                  setNavOpen(false);
                }}
              >
                <Icon name="bookmark" />
                全部网站
              </button>
              <button
                class={
                  route.url.searchParams.has("starred") ? s.navSelected : ""
                }
                onClick={() => {
                  route.navigate("/library?starred=true");
                  setNavOpen(false);
                }}
              >
                <Icon name="star" />
                星标收藏
              </button>
              <button
                class={
                  route.url.searchParams.has("unclassified")
                    ? s.navSelected
                    : ""
                }
                onClick={() => {
                  route.navigate("/library?unclassified=true");
                  setNavOpen(false);
                }}
              >
                <Icon name="folder" />
                未分类
              </button>
            </div>
            <div class={s.sectionHeading}>
              <span>文件夹</span>
              <Button
                icon="plus"
                title="新建文件夹"
                onClick={() => setFolderDialog("new")}
              />
            </div>
            <ErrorNotice
              message={foldersQuery.error}
              retry={foldersQuery.reload}
            />
            <FolderTree
              folders={folders}
              parent={null}
              selected={selectedFolder}
              onSelect={(id) => {
                route.navigate(`/library?folder=${id}&scope=folder`);
                setNavOpen(false);
              }}
              onManage={setFolderDialog}
            />
            <div class={s.sectionHeading}>
              <span>标签</span>
              <Button
                icon="plus"
                title="管理标签"
                onClick={() => setTagDialog(true)}
              />
            </div>
            <ErrorNotice message={tagsQuery.error} retry={tagsQuery.reload} />
            {tags.length ? (
              <ul class={s.tags}>
                {tags.map((tag) => (
                  <li key={tag.id}>
                    <button
                      onClick={() => {
                        route.navigate(`/library?tags=${tag.id}`);
                        setNavOpen(false);
                      }}
                      class={
                        route.url.searchParams.get("tags") === tag.id
                          ? s.navSelected
                          : ""
                      }
                    >
                      <Icon name="tag" />
                      {tag.name}
                      <span>{tag.count ?? ""}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p class={s.noItems}>暂无标签</p>
            )}
            <nav class={s.sidePages} aria-label="工具页面">
              {navigation.slice(1).map((n) => (
                <a
                  href={n.path}
                  key={n.path}
                  onClick={(e) => {
                    if (!e.ctrlKey && !e.metaKey && !e.shiftKey) {
                      e.preventDefault();
                      route.navigate(n.path);
                    }
                  }}
                >
                  <Icon name={n.icon} />
                  {n.label}
                </a>
              ))}
            </nav>
            </ResizableSidebar>
          <LibraryWorkspace active={page === "/library"} />
          {page === "/library" ? null : page === "/settings" ? (
            <SettingsPage />
          ) : page === "/transfers" ? (
            <Transfers />
          ) : (
            <div class={s.content}>
              <Empty title="页面不存在">
                <Button onClick={() => route.navigate("/library")}>
                  返回网站库
                </Button>
              </Empty>
            </div>
          )}
        </div>
        <div class={s.taskDock}>
          {activeImport && (
            <button class={s.taskSummary} onClick={() => { setNavOpen(false); route.navigate("/transfers"); }}>
              <Icon name="upload" />
              <span>导入工作流程<small>{activeImport.progress}% · 抓取 {activeImport.capture_completed} / {activeImport.capture_total}</small></span>
            </button>
          )}
          <button
            class={s.taskSummary}
            aria-label="后台任务"
            title="查看后台任务和分类建议"
            onClick={() => {
              setNavOpen(false);
              setTaskPage(1);
              if (currentTask?.id) selectGroup(currentTask.id);
              setShowProgress(true);
            }}
          >
            <Icon name="refresh" />
            <span>
              {currentTask ? `${jobName(currentTask.kind)} · ${stateLabel(currentTask.state)}` : "后台任务"}
              <small>
                {currentTask
                  ? `${currentTask.completed ?? 0} / ${currentTask.total ?? 0}`
                  : tasks.loading ? "连接进度…" : "暂无任务"}
                {!!tasks.data?.pending_proposals && ` · 全库待校对 ${tasks.data.pending_proposals}`}
              </small>
            </span>
          </button>
        </div>
        {reviewScope && (
          <ClassificationReview scope={reviewScope} onClose={() => setReviewScope(null)} />
        )}
        {toast && (
          <div class={s.toast} role="status">
            {toast}
            <button aria-label="关闭通知" onClick={() => setToast("")}>
              <Icon name="close" size={16} />
            </button>
          </div>
        )}
        {folderDialog && (
          <FolderDialog
            folder={folderDialog === "new" ? null : folderDialog}
            folders={folders}
            onClose={() => setFolderDialog(null)}
            onDone={() => {
              setFolderDialog(null);
              changed();
            }}
          />
        )}
        {tagDialog && (
          <TagDialog
            tags={tags}
            onClose={() => setTagDialog(false)}
            onDone={changed}
          />
        )}
        {showProgress && (
          <Dialog title="后台任务" onClose={() => setShowProgress(false)} wide>
            <ErrorNotice message={tasks.error} retry={tasks.reload} />
            <div class={s.taskToolbar}>
              <label>
                任务批次
                <select aria-label="任务批次" value={group ?? ""} onChange={(e) => selectGroup(e.currentTarget.value)}>
                  {!group && <option value="">选择任务</option>}
                  {group && !tasks.data?.groups.some((item) => item.id === group) && <option value={group}>当前任务 · {group.slice(0, 8)}</option>}
                  {tasks.data?.groups.map((item) => (
                    <option key={item.id} value={item.id}>
                      {jobName(item.kind)} · {item.requested_count} 个网站 · {stateLabel(item.state)} · {item.created_at ? new Date(item.created_at).toLocaleString() : ""} · {item.id?.slice(0, 8)}
                    </option>
                  ))}
                </select>
              </label>
              <Button disabled={taskPage === 1} onClick={() => setTaskPage((value) => value - 1)}>上一组任务</Button>
              <span class="muted">{taskPage} / {Math.max(1, Math.ceil((tasks.page?.total ?? 0) / 20))}</span>
              <Button disabled={taskPage * 20 >= (tasks.page?.total ?? 0)} onClick={() => setTaskPage((value) => value + 1)}>下一组任务</Button>
              <Button onClick={() => { tasks.reload(); progress.reload(); }}>刷新任务列表</Button>
              <Button disabled={!tasks.data?.pending_proposals} onClick={() => { setShowProgress(false); setReviewScope({}); }}>
                校对分类（{tasks.data?.pending_proposals ?? 0}）
              </Button>
            </div>
            <p class="muted">{tasks.data?.active_count ?? 0} 组任务仍在后台处理。刷新页面后可在此继续查看进度和分类结果。</p>
            {!group && <Empty title={tasks.loading ? "正在读取后台任务…" : "暂无后台任务"} />}
            <ErrorNotice message={progress.error} retry={progress.reload} />
            {group && <p>{stateLabel(progress.data?.state ?? listedTask?.state ?? (currentTask?.id === group ? currentTask.state : "queued"))}</p>}
            {progress.data && (
              <>
                <p>
                  已完成{" "}
                  {progress.data.completed ??
                    progress.data.completed_count ??
                    progress.data.device_completed ??
                    0}{" "}
                  /{" "}
                  {progress.data.total ??
                    progress.data.device_total ??
                    progress.data.requested_count ??
                  0}{" "}
                  {progress.data.kind === "classification" && "批次 "}
                  · 失败{" "}
                  {progress.data.failed ??
                    progress.data.failed_count ??
                    progress.data.counts?.failed ??
                    0}{" "}
                  · 跳过 {progress.data.skipped ?? 0}
                </p>
                {progress.data.kind === "classification" && (
                  <>
                    {progress.data.phase === "planning" && ["queued", "running", "retry_wait"].includes(progress.data.state) && (
                      <p class="muted">正在规划全库统一分类目录，完成后按此目录逐批归类。</p>
                    )}
                    <p>
                      网站已处理 {progress.data.website_completed ?? 0} / {progress.data.website_total ?? progress.data.requested_count ?? 0}
                      {" "}· 成功 {progress.data.succeeded_sites ?? 0} · 失败 {progress.data.failed_sites ?? 0}
                      {" "}· 取消 {progress.data.cancelled_sites ?? 0}
                    </p>
                    <p class="muted">
                      每个 AI 批次最多处理 10 个网站。待执行 {progress.data.queued_sites ?? 0}
                      {" "}· 正在处理 {progress.data.running_sites ?? 0} · 等待重试 {progress.data.retrying_sites ?? 0}
                    </p>
                    {progress.data.state === "queued" && (
                      <p class="muted">正在排队，等待后台工作进程处理；较早提交的任务会先执行。</p>
                    )}
                    {!!progress.data.retrying_sites && (
                      <p class="status-bad">部分 AI 请求失败，正在等待自动重试。具体原因见下方任务状态。</p>
                    )}
                  </>
                )}
                {progress.data.kind === "refresh" && (
                  <p class="muted">
                    网站已完成 {progress.data.completed_sites ?? 0} /{" "}
                    {progress.data.requested_count ?? 0} · 双端成功{" "}
                    {progress.data.succeeded_sites ?? 0} · 部分成功{" "}
                    {progress.data.partial_sites ?? 0} · 失败{" "}
                    {progress.data.failed_sites ?? 0}
                  </p>
                )}
                <progress
                  value={
                    progress.data.completed ??
                    progress.data.completed_count ??
                    progress.data.device_completed ??
                    0
                  }
                  max={Math.max(
                    1,
                    progress.data.total ??
                      progress.data.device_total ??
                      progress.data.requested_count ??
                      1,
                  )}
                />
                {progress.data.jobs && (
                  <div class="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>任务</th>
                          <th>设备</th>
                          <th>状态</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {progress.data.jobs.map((job) => (
                          <tr key={job.id}>
                            <td>{jobName(job.kind ?? progress.data?.kind)}{job.website_count != null && `（${job.website_count} 个网站）`}</td>
                            <td>
                              {job.variant === "mobile"
                                ? "手机"
                                : job.variant === "desktop"
                                  ? "桌面"
                                  : "—"}
                            </td>
                            <td>
                              {["retry_wait", "failed"].includes(job.state)
                                ? <span class="status-bad">{errorCodeLabel(job.error_code || "TASK_FAILED")}</span>
                                : stateLabel(job.state)}
                            </td>
                            <td>
                              {["retry_wait", "failed", "cancelled"].includes(job.state) ? (
                                <Button
                                  onClick={() => {
                                    void request(`/jobs/${job.id}/retry`, {
                                      method: "POST",
                                      body: {},
                                    })
                                      .then(() => { progress.reload(); tasks.reload(); changed(); })
                                      .catch((e) => notify(errorMessage(e)));
                                  }}
                                >
                                  重试
                                </Button>
                              ) : (
                                !terminalState(job.state) && (
                                  <Button
                                    onClick={() => {
                                      void request(`/jobs/${job.id}/cancel`, {
                                        method: "POST",
                                        body: {},
                                      })
                                        .then(() => progress.reload())
                                        .catch((e) => notify(errorMessage(e)));
                                    }}
                                  >
                                    取消
                                  </Button>
                                )
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </>
            )}
          </Dialog>
        )}
      </div>
      </PreviewSession>
    </AppState.Provider>
  );
}
function jobName(kind?: string) {
  return (
    (
      {
        capture: "网页快照",
        refresh: "网页快照",
        classification: "AI 分类",
        classify: "AI 分类",
        ai_test: "AI 连接验证",
        import: "浏览器导入",
        backup: "整库备份",
        restore: "覆盖恢复",
      } as Record<string, string>
    )[kind ?? ""] ?? "后台任务"
  );
}
function FolderTree({
  folders,
  parent,
  selected,
  onSelect,
  onManage,
  depth = 0,
}: {
  folders: Folder[];
  parent: string | null;
  selected: string | null;
  onSelect: (id: string) => void;
  onManage: (folder: Folder) => void;
  depth?: number;
}) {
  const [closed, setClosed] = useState<Set<string>>(new Set());
  return (
    <ul class={s.folderTree}>
      {folders
        .filter((f) => f.parent_id === parent)
        .map((folder) => {
          const children = folders.some((f) => f.parent_id === folder.id);
          const open = !closed.has(folder.id);
          return (
            <li key={folder.id}>
              <div
                class={`${s.folderRow} ${selected === folder.id ? s.navSelected : ""}`}
                style={{ paddingLeft: `${8 + depth * 14}px` }}
              >
                <button
                  class={`${s.expand} ${open ? s.rotated : ""}`}
                  onClick={() =>
                    setClosed((old) => {
                      const next = new Set(old);
                      open ? next.add(folder.id) : next.delete(folder.id);
                      return next;
                    })
                  }
                  disabled={!children}
                  aria-label={`${open ? "折叠" : "展开"} ${folder.name}`}
                  aria-expanded={children ? open : undefined}
                >
                  {children && <Icon name="chevron" size={13} />}
                </button>
                <button
                  class={s.folderSelect}
                  onClick={() => onSelect(folder.id)}
                  title={folderPath(folders, folder.id)}
                >
                  <Icon name="folder" size={18} />
                  <span>{folder.name}</span>
                  <small>{folder.count ?? folder.bookmark_count ?? ""}</small>
                </button>
                <button
                  class={s.folderManage}
                  aria-label={`管理文件夹 ${folder.name}`}
                  onClick={() => onManage(folder)}
                >
                  <Icon name="edit" size={13} />
                </button>
              </div>
              {children && open && (
                <FolderTree
                  folders={folders}
                  parent={folder.id}
                  selected={selected}
                  onSelect={onSelect}
                  onManage={onManage}
                  depth={depth + 1}
                />
              )}
            </li>
          );
        })}
    </ul>
  );
}
function FolderDialog({
  folder,
  folders,
  onClose,
  onDone,
}: {
  folder: Folder | null;
  folders: Folder[];
  onClose: () => void;
  onDone: () => void;
}) {
  const previews = usePreviewDocuments();
  const [name, setName] = useState(folder?.name ?? "");
  const [parent, setParent] = useState(folder?.parent_id ?? "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  async function save(e: Event) {
    e.preventDefault();
    setBusy(true);
    try {
      await request(folder ? `/folders/${folder.id}` : "/folders", {
        method: folder ? "PATCH" : "POST",
        body: {
          name,
          parent_id: parent || null,
          ...(folder ? { revision: folder.revision } : {}),
        },
      });
      onDone();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!folder) return;
    setBusy(true);
    setError("");
    try {
      if (!preview) {
        const { data } = await request<Record<string, unknown>>(
          `/folders/${folder.id}/delete-preview`,
          { method: "POST", body: {} },
        );
        setPreview(data);
      } else {
        await request(`/folders/${folder.id}`, {
          method: "DELETE",
          body: {
            revision: folder.revision,
            preview_token: preview.token ?? preview.preview_token,
          },
        });
        // Reconcile cached owners with the authoritative remaining catalog.
        // A failed follow-up read must not turn a completed deletion into an error.
        void request<{ id: string }[]>("/bookmarks?selection=true")
          .then(({ data }) => previews.retainBookmarks(data.map((item) => item.id)))
          .catch(() => {});
        onDone();
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title={folder ? "管理文件夹" : "新建文件夹"} onClose={onClose}>
      <form onSubmit={save}>
        <ErrorNotice message={error} />
        {preview ? (
          <>
            <p>将删除「{folder?.name}」及其全部子文件夹。</p>
            <p>
              受影响网站：
              {String(preview.bookmark_count ?? preview.bookmarks ?? 0)} ·
              快照：{String(preview.snapshot_count ?? preview.versions ?? 0)}
            </p>
            <p class="status-bad">其中的网站和全部历史也会永久删除。</p>
          </>
        ) : (
          <>
            <Field label="名称">
              <input
                autoFocus
                required
                maxLength={100}
                value={name}
                onInput={(e) => setName(e.currentTarget.value)}
              />
            </Field>
            <Field label="父文件夹">
              <select
                value={parent}
                onChange={(e) => setParent(e.currentTarget.value)}
              >
                <option value="">根目录</option>
                {folders
                  .filter((f) => f.id !== folder?.id)
                  .map((f) => (
                    <option key={f.id} value={f.id}>
                      {folderPath(folders, f.id)}
                    </option>
                  ))}
              </select>
            </Field>
          </>
        )}
        <div class="form-actions">
          {folder && (
            <Button danger onClick={() => void remove()} disabled={busy}>
              {preview ? "确认删除全部" : "删除文件夹"}
            </Button>
          )}
          <Button onClick={() => (preview ? setPreview(null) : onClose())}>
            {preview ? "返回" : "取消"}
          </Button>
          {!preview && (
            <Button primary type="submit" disabled={busy}>
              保存
            </Button>
          )}
        </div>
      </form>
    </Dialog>
  );
}
function TagDialog({
  tags,
  onClose,
  onDone,
}: {
  tags: Tag[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const tag = tags.find((t) => t.id === id);
  async function save(e: Event) {
    e.preventDefault();
    setBusy(true);
    try {
      await request(tag ? `/tags/${tag.id}` : "/tags", {
        method: tag ? "PATCH" : "POST",
        body: { name, ...(tag ? { revision: tag.revision } : {}) },
      });
      onDone();
      setName("");
      setId("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (!tag || !confirm(`删除标签「${tag.name}」？网站记录会保留。`)) return;
    setBusy(true);
    try {
      await request(`/tags/${tag.id}`, {
        method: "DELETE",
        body: { revision: tag.revision },
      });
      onDone();
      setName("");
      setId("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title="管理标签" onClose={onClose}>
      <form onSubmit={save}>
        <ErrorNotice message={error} />
        <Field label="选择标签">
          <select
            value={id}
            onChange={(e) => {
              const value = e.currentTarget.value;
              setId(value);
              setName(tags.find((t) => t.id === value)?.name ?? "");
            }}
          >
            <option value="">新建标签</option>
            {tags.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="标签名称">
          <input
            required
            maxLength={100}
            value={name}
            onInput={(e) => setName(e.currentTarget.value)}
          />
        </Field>
        <div class="form-actions">
          {tag && (
            <Button danger onClick={() => void remove()} disabled={busy}>
              删除标签
            </Button>
          )}
          <Button onClick={onClose}>关闭</Button>
          <Button primary type="submit" disabled={busy}>
            保存
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
