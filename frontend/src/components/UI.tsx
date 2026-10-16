import type { ComponentChildren } from "preact";
import { useEffect, useRef } from "preact/hooks";
import s from "./UI.module.css";
let scrollLocks = 0;
export function lockBodyScroll() {
  scrollLocks++;
  document.body.classList.add("modal-open");
  let released = false;
  return () => {
    if (released) return;
    released = true;
    scrollLocks = Math.max(0, scrollLocks - 1);
    if (!scrollLocks) document.body.classList.remove("modal-open");
  };
}
export type IconName =
  | "bookmark"
  | "folder"
  | "star"
  | "search"
  | "plus"
  | "refresh"
  | "tag"
  | "settings"
  | "external"
  | "close"
  | "menu"
  | "check"
  | "chevron"
  | "grid"
  | "upload"
  | "download"
  | "archive"
  | "clock"
  | "alert"
  | "edit"
  | "trash"
  | "arrow"
  | "desktop"
  | "mobile"
  | "logout";
const paths: Record<IconName, ComponentChildren> = {
  bookmark: <path d="M6 3h12v18l-6-4-6 4z" />,
  folder: <path d="M3 7V5h7l2 2h9v13H3z" />,
  star: (
    <path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z" />
  ),
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="m16 16 5 5" />
    </>
  ),
  plus: <path d="M12 4v16M4 12h16" />,
  refresh: (
    <>
      <path d="M20 8a8 8 0 1 0 1 8M20 3v5h-5" />
    </>
  ),
  tag: (
    <>
      <path d="M3 3h8l10 10-8 8L3 11z" />
      <circle cx="7.5" cy="7.5" r=".5" />
    </>
  ),
  settings: (
    <>
      <path d="m9 3 6 0 1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  external: (
    <>
      <path d="M14 3h7v7M21 3l-10 10M10 4H4v16h16v-6" />
    </>
  ),
  close: <path d="m5 5 14 14M19 5 5 19" />,
  menu: <path d="M3 6h18M3 12h18M3 18h18" />,
  check: <path d="m4 12 5 5L20 6" />,
  chevron: <path d="m8 4 8 8-8 8" />,
  grid: (
    <>
      <path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z" />
    </>
  ),
  upload: <path d="M12 16V3m-5 5 5-5 5 5M3 16v5h18v-5" />,
  download: <path d="M12 3v13m-5-5 5 5 5-5M3 17v4h18v-4" />,
  archive: <path d="M3 3h18v5H3zM5 8v13h14V8M9 12h6" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l4 2" />
    </>
  ),
  alert: (
    <>
      <path d="m12 3 10 18H2zM12 9v5" />
      <circle cx="12" cy="17" r=".5" />
    </>
  ),
  edit: <path d="m14 4 6 6M3 21l2-7L17 2l5 5L10 19z" />,
  trash: <path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" />,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  desktop: <path d="M3 3h18v14H3zM12 17v4M7 21h10" />,
  mobile: (
    <>
      <path d="M7 2h10v20H7z" />
      <path d="M11 18h2" />
    </>
  ),
  logout: <path d="M10 3H3v18h7M9 12h12m-5-5 5 5-5 5" />,
};
export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  );
}
export function Button({
  children,
  onClick,
  primary,
  danger,
  disabled,
  type = "button",
  title,
  icon,
  className = "",
}: {
  children?: ComponentChildren;
  onClick?: () => void;
  primary?: boolean;
  danger?: boolean;
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
  icon?: IconName;
  className?: string;
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      class={`${s.button} ${primary ? s.primary : ""} ${danger ? s.danger : ""} ${!children ? s.iconOnly : ""} ${className}`}
    >
      {icon && <Icon name={icon} />} {children}
    </button>
  );
}
export function ErrorNotice({
  message,
  retry,
}: {
  message?: string;
  retry?: () => void;
}) {
  return message ? (
    <div class={s.error} role="alert">
      <Icon name="alert" />
      <span>{message}</span>
      {retry && <Button onClick={retry}>重新加载</Button>}
    </div>
  ) : null;
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ComponentChildren;
}) {
  return (
    <div class={s.empty}>
      <Icon name="bookmark" size={32} />
      <h3>{title}</h3>
      {children}
    </div>
  );
}
export function Dialog({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: ComponentChildren;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const prior = document.activeElement as HTMLElement | null;
    const dialog = ref.current!;
    dialog.showModal();
    const unlock = lockBodyScroll();
    return () => {
      dialog.close();
      unlock();
      prior?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      class={`${s.dialog} ${wide ? s.wide : ""}`}
      onCancel={(e) => {
        e.preventDefault();
        closeRef.current();
      }}
      onClick={(e) => {
        if (
          e.target === ref.current &&
          (e.clientX < e.currentTarget.getBoundingClientRect().left ||
            e.clientX > e.currentTarget.getBoundingClientRect().right ||
            e.clientY < e.currentTarget.getBoundingClientRect().top ||
            e.clientY > e.currentTarget.getBoundingClientRect().bottom)
        )
          closeRef.current();
      }}
      aria-label={title}
    >
      <header class={s.dialogHeader}>
        <h2>{title}</h2>
        <Button icon="close" title="关闭" onClick={onClose} />
      </header>
      <div class={s.dialogBody}>{children}</div>
    </dialog>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ComponentChildren;
  hint?: string;
}) {
  return (
    <label class={s.field}>
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export function Time({ value }: { value?: string | null }) {
  if (!value) return <span>尚未生成</span>;
  const date = new Date(value);
  return (
    <time dateTime={value} title={date.toLocaleString("zh-CN")}>
      {date.toLocaleDateString("zh-CN")}{" "}
      {date.toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })}
    </time>
  );
}
export const terminalState = (state?: string) =>
  ["succeeded", "failed", "cancelled", "completed", "partial"].includes(
    state ?? "",
  );
export function errorCodeLabel(code: string) {
  return (
    (
      {
        CAPTURE_BLOCKED: "页面地址被安全策略拦截",
        unsafe_url: "页面地址被安全策略拦截",
        CAPTURE_TIMEOUT: "网页加载超时",
        CAPTURE_NETWORK: "无法连接网页",
          CAPTURE_PROXY_UNAVAILABLE: "无法连接网页缓存代理，请检查代理是否运行",
          CAPTURE_PROXY_CONFIG: "网页缓存代理配置无效，请检查代理设置",
          CAPTURE_ACCESS_DENIED: "网站拒绝访问",
          CAPTURE_NOT_FOUND: "网页不存在或已被移除",
          CAPTURE_CHALLENGE: "网站要求人机验证，暂时无法缓存",
          CAPTURE_DOM_TOO_LARGE: "网页结构过大，超过缓存处理上限",
        CAPTURE_TOOL_MISSING: "网页采集组件未安装",
        CAPTURE_ISOLATION_REQUIRED: "网页采集隔离环境未就绪",
        CAPTURE_TOO_LARGE: "网页内容超过采集上限",
        CAPTURE_UNSUPPORTED: "该页面暂不支持完整归档",
        CAPTURE_REPLAY_FAILED: "网页归档校验失败",
        AI_TIMEOUT: "AI 服务响应超时",
        AI_RATE_LIMITED: "AI 服务调用频率受限",
        AI_UNCONFIGURED: "请先配置并验证 AI 服务",
        AI_INVALID_RESULT: "AI 服务返回的分类结果无效",
        AI_CONFIG_STALE: "AI 配置已改变，请重新验证",
        AI_CREDENTIAL_REBIND_REQUIRED: "AI 地址已改变，请重新提供并验证密钥",
        NETWORK_ERROR: "网络连接失败",
        TASK_FAILED: "任务执行失败",
        LEASE_EXPIRED: "任务执行中断",
      } as Record<string, string>
    )[code] ?? code
  );
}
export const stateLabel = (state?: string) =>
  ({
    queued: "等待执行",
    running: "正在执行",
    retry_wait: "等待重试",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
    pending: "待确认",
    accepted: "已确认",
    rejected: "已驳回",
    stale: "已过期",
    validating: "正在校验",
    awaiting_confirmation: "等待覆盖确认",
    partial: "部分成功",
    completed: "已完成",
    skipped: "已跳过",
    missing: "尚无快照",
    disabled: "快照已关闭",
    ready: "已有成功快照",
    previewed: "预检完成",
    importing: "正在导入",
    valid: "有效",
    duplicate: "重复跳过",
    invalid: "无效条目",
  })[state ?? ""] ??
  state ??
  "等待快照";
