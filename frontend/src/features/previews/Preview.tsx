import { createContext, type ComponentChildren } from "preact";
import { useContext, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import { Icon } from "../../components/UI";
import type { Snapshot } from "../../api/types";
import { PreviewDocuments, type PreviewState } from "./PreviewDocuments";
import s from "./Preview.module.css";

const Documents = createContext<PreviewDocuments | null>(null);

/** Keep image surfaces mounted for the preview session, even off-route. */
export function PreviewLayer() {
  return <div class={s.layer} data-preview-layer="" aria-hidden="true" />;
}

/** One lifetime per authenticated library; navigation does not own documents. */
export function PreviewSession({ children }: { children: ComponentChildren }) {
  const documents = useMemo(() => new PreviewDocuments(), []);
  useLayoutEffect(() => () => documents.dispose(), [documents]);
  return <Documents.Provider value={documents}>{children}</Documents.Provider>;
}

export function usePreviewDocuments() {
  const documents = useContext(Documents);
  if (!documents) throw new Error("Missing preview session");
  return documents;
}

export function Preview({ snapshot, bookmarkId, detail = false, disabled = false,
  fallback = "尚无快照", title = "网页快照",
}: {
  snapshot?: Snapshot | null;
  bookmarkId: string;
  detail?: boolean;
  disabled?: boolean;
  fallback?: string;
  title?: string;
}) {
  const documents = usePreviewDocuments();
  const container = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<PreviewState>("queued");
  useLayoutEffect(() => {
    if (!snapshot) return;
    return documents.attach(snapshot, bookmarkId, {
      element: container.current!, detail, disabled, title, notify: setState,
    });
  }, [documents, snapshot?.thumbnail_url, snapshot?.version_id, snapshot?.variant,
    bookmarkId, detail, disabled, title]);
  useLayoutEffect(() => documents.schedule());
  const failed = !!snapshot && (state === "timeout" || state === "error");
  return (
    <div ref={container} class={`${s.preview} ${detail ? s.detail : ""}`}
      style={detail && snapshot
        ? { aspectRatio: `${snapshot.viewport_width || 1366}/${snapshot.viewport_height || 768}` }
        : undefined}
      data-preview-container="" data-preview-state={snapshot ? state : "missing"}>
      <div class={s.placeholder}>
        <Icon name={snapshot ? "clock" : "grid"} size={24} />
        <span>{snapshot ? (failed ? "预览未加载" : state === "generating" ? "正在生成预览" : "快照预览") : fallback}</span>
      </div>
      {failed && <span class={s.timeout}>预览未加载</span>}
    </div>
  );
}
