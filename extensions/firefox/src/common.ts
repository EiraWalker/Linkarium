namespace Linkarium {
  export const ORIGIN = "https://link.eirawav.me";
  // WebExtension match patterns omit ports; the bridge separately checks the exact origin.
  export const SITE_PATTERN = new URL(ORIGIN).protocol + "//" + new URL(ORIGIN).hostname + "/*";
  export const PREFIX = "linkarium:";
  export interface Submission {
    submission_id: string;
    bookmark_id: string | null;
    created: boolean;
    state: string;
    revision: number;
    folder_path: string;
    error_code: string;
    error_message: string;
    can_retry: boolean;
    group_id: string | null;
  }
  export interface Operation {
    id: string;
    url: string;
    title: string;
    createdAt: number;
    state: "sending" | "login_required" | "unconfirmed" | "accepted";
    result?: Submission;
    message?: string;
    recoveryTried?: boolean;
    restartRecoveryTried?: boolean;
    retry?: {key: string; revision: number};
  }
  export interface BridgeRequest {
    kind: "linkarium-api";
    action: "submit" | "status" | "retry";
    key: string;
    url?: string;
    title?: string;
    submissionId?: string;
    revision?: number;
  }
  export type Reply = {ok: true; data: Submission} | {ok: false; code: string; message: string};
  export function pageUrl(value: string): string {
    const url = new URL(value);
    if (!/^https?:$/.test(url.protocol) || url.username || url.password || value.length > 8192 || /[\x00-\x1f]/.test(value)) {
      throw new Error("仅支持不含登录凭据的 HTTP/HTTPS 网页");
    }
    return value;
  }
  export function label(operation: Operation): string {
    if (operation.state === "login_required") return "请先登录 Linkarium";
    if (operation.state === "unconfirmed") return operation.message || "提交结果待确认";
    if (!operation.result) return "正在提交";
    if (operation.result.state === "classified") return "已自动归档";
    if (operation.result.state === "already_saved") return "已存在";
    if (operation.result.state === "needs_attention") return "已保存，分类需要处理";
    return "已保存，正在分类";
  }
}
