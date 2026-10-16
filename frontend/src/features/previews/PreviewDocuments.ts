import type { Snapshot } from "../../api/types";
import s from "./Preview.module.css";

export type PreviewState = "queued" | "loading" | "generating" | "ready" | "timeout" | "error";
type View = {
  element: HTMLElement;
  detail: boolean;
  disabled: boolean;
  title: string;
  notify: (state: PreviewState) => void;
};
type PreviewDocument = {
  key: string;
  snapshot: Snapshot;
  bookmarkId: string;
  root: HTMLElement;
  state: PreviewState;
  view?: View;
  surface?: HTMLDivElement;
  image?: HTMLImageElement;
  timer?: ReturnType<typeof setTimeout>;
  priority: number;
};
type ThumbnailResult =
  | { state: "ready"; url: string }
  | { state: "generating"; retryAfter: number }
  | { state: "error" | "timeout" };
type ThumbnailRequest = {
  controller: AbortController;
  promise: Promise<ThumbnailResult>;
};

/**
 * Keeps first-screen images for the authenticated library session. Cards and
 * details share one image download; temporary anchors do not own the cache.
 */
export class PreviewDocuments {
  private documents = new Set<PreviewDocument>();
  private disposed = false;
  private pending = false;
  private resize?: ResizeObserver;
  private images = new Map<string, string>();
  private requests = new Map<string, ThumbnailRequest>();

  attach(snapshot: Snapshot, bookmarkId: string, view: View) {
    const url = this.safeUrl(snapshot.thumbnail_url);
    if (!url || this.disposed) {
      view.notify("error");
      return;
    }
    const key = JSON.stringify([snapshot.version_id, snapshot.variant, url]);
    const root = view.element.closest<HTMLElement>("[data-preview-root]");
    if (!root) throw new Error("Missing persistent preview scroll root");
    // Card/detail views have separate surfaces but share the same cached image.
    // Later visits reuse a surface within the same persistent scroll region.
    let doc = [...this.documents].find((item) => item.key === key && item.root === root && !item.view);
    if (!doc) {
      doc = { key, snapshot: { ...snapshot, thumbnail_url: url }, bookmarkId, root,
        state: "queued", priority: 0 };
      this.documents.add(doc);
    }
    doc.view = view;
    if (doc.image) doc.image.alt = view.title;
    view.notify(doc.state);
    if (!this.resize) {
      this.resize = new ResizeObserver(this.schedule);
      window.addEventListener("resize", this.schedule);
    }
    this.resize.observe(view.element);
    this.schedule();
    const attached = doc;
    return () => {
      this.resize?.unobserve(view.element);
      if (attached.view !== view) return;
      attached.view = undefined;
      if (attached.surface) attached.surface.hidden = true;
      // Unstarted work belongs to the page; loaded images belong to the session.
      if (attached.state === "queued" || attached.state === "generating") this.remove(attached);
      this.schedule();
    };
  }

  schedule = () => {
    if (this.pending || this.disposed) return;
    this.pending = true;
    queueMicrotask(() => {
      this.pending = false;
      if (this.disposed) return;
      this.layout();
      this.loadNext();
    });
  };

  forgetBookmarks(ids: Iterable<string>) {
    const removed = new Set(ids);
    for (const doc of this.documents)
      if (removed.has(doc.bookmarkId)) this.remove(doc);
    this.schedule();
  }

  retainBookmarks(ids: Iterable<string>) {
    const remaining = new Set(ids);
    this.forgetBookmarks([...this.documents].map((doc) => doc.bookmarkId)
      .filter((id) => !remaining.has(id)));
  }

  forgetVersions(ids: Iterable<string>) {
    const removed = new Set(ids);
    for (const doc of this.documents)
      if (removed.has(doc.snapshot.version_id) || (doc.snapshot.id && removed.has(doc.snapshot.id)))
        this.remove(doc);
    this.schedule();
  }

  dispose() {
    this.disposed = true;
    this.resize?.disconnect();
    window.removeEventListener("resize", this.schedule);
    for (const doc of this.documents) this.remove(doc);
    for (const request of this.requests.values()) request.controller.abort();
    this.requests.clear();
    for (const url of this.images.values()) URL.revokeObjectURL(url);
    this.images.clear();
  }

  private safeUrl(url?: string) {
    if (!url) return undefined;
    try {
      const parsed = new URL(url, location.origin);
      return parsed.origin === location.origin && parsed.pathname.startsWith("/api/v1/snapshots/")
        ? parsed.href : undefined;
    } catch { return undefined; }
  }

  private loadNext() {
    const concurrency = matchMedia("(max-width:767px)").matches ? 1 : 2;
    let loading = [...this.documents].filter((doc) => doc.state === "loading").length;
    const queued = [...this.documents]
      .filter((doc) => doc.state === "queued" && doc.view && !doc.view.disabled)
      .sort((a, b) => b.priority - a.priority);
    for (const doc of queued) {
      if (loading >= concurrency) break;
      loading++;
      this.load(doc);
    }
  }

  private load(doc: PreviewDocument) {
    doc.state = "loading";
    doc.view?.notify(doc.state);
    void this.thumbnail(doc.snapshot.thumbnail_url!).then((result) => {
      if (!this.documents.has(doc) || doc.state !== "loading" || this.disposed) return;
      if (result.state === "ready") {
        this.mount(doc, result.url);
        return;
      }
      this.finish(doc, result.state);
      if (result.state === "generating") {
        if (!doc.view) { this.remove(doc); return; }
        doc.timer = setTimeout(() => {
          if (!this.documents.has(doc) || this.disposed) return;
          doc.state = "queued";
          this.schedule();
        }, result.retryAfter);
      }
    });
  }

  private thumbnail(url: string): Promise<ThumbnailResult> {
    const cached = this.images.get(url);
    if (cached) return Promise.resolve({ state: "ready", url: cached });
    const pending = this.requests.get(url);
    if (pending) return pending.promise;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    const promise = (async (): Promise<ThumbnailResult> => {
      try {
        const response = await fetch(url, {
          credentials: "same-origin", signal: controller.signal,
          headers: { Accept: "image/jpeg" },
        });
        if (response.status === 401) window.dispatchEvent(new Event("session-expired"));
        if (response.status === 202) {
          const seconds = Number(response.headers.get("Retry-After"));
          return { state: "generating", retryAfter: Math.min(30, Math.max(2, seconds || 5)) * 1000 };
        }
        if (!response.ok || response.headers.get("Content-Type")?.split(";")[0] !== "image/jpeg")
          return { state: "error" };
        const blob = await response.blob();
        if (this.disposed || ![...this.documents].some((doc) => doc.snapshot.thumbnail_url === url))
          return { state: "error" };
        const objectUrl = URL.createObjectURL(blob);
        this.images.set(url, objectUrl);
        return { state: "ready", url: objectUrl };
      } catch {
        return { state: controller.signal.aborted ? "timeout" : "error" };
      } finally {
        clearTimeout(timer);
        if (this.requests.get(url)?.controller === controller) this.requests.delete(url);
      }
    })();
    this.requests.set(url, { controller, promise });
    return promise;
  }

  private mount(doc: PreviewDocument, url: string) {
    const surface = document.createElement("div");
    surface.className = s.document;
    surface.hidden = true;
    surface.setAttribute("aria-hidden", "true");
    const image = document.createElement("img");
    image.className = s.image;
    image.alt = doc.view?.title ?? "网页快照";
    image.setAttribute("aria-hidden", "true");
    image.setAttribute("data-preview-image", doc.snapshot.version_id);
    image.decoding = "async";
    image.draggable = false;
    doc.surface = surface;
    doc.image = image;
    // load releases the queue slot; capture success remains backend state.
    image.onload = () => this.finish(doc, "ready");
    image.onerror = () => this.finish(doc, "error");
    image.src = url;
    surface.append(image);
    doc.root.querySelector(":scope > [data-preview-layer]")!.append(surface);
  }

  private finish(doc: PreviewDocument, state: PreviewState) {
    if (!this.documents.has(doc) || this.disposed) return;
    clearTimeout(doc.timer);
    doc.state = state;
    doc.view?.notify(state);
    this.schedule();
  }

  private remove(doc: PreviewDocument) {
    clearTimeout(doc.timer);
    if (doc.image) doc.image.onload = doc.image.onerror = null;
    doc.surface?.remove();
    if (!this.disposed) doc.view?.notify("error");
    this.documents.delete(doc);
    const url = doc.snapshot.thumbnail_url!;
    if (![...this.documents].some((item) => item.snapshot.thumbnail_url === url)) {
      this.requests.get(url)?.controller.abort();
      this.requests.delete(url);
      const objectUrl = this.images.get(url);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      this.images.delete(url);
    }
  }

  private layout() {
    for (const doc of this.documents) {
      const view = doc.view;
      const surface = doc.surface;
      if (!view || !view.element.isConnected) {
        if (surface) surface.hidden = true;
        continue;
      }
      const rect = view.element.getBoundingClientRect();
      const style = getComputedStyle(view.element);
      const root = doc.root;
      const origin = root.getBoundingClientRect();
      // Coordinates belong to the scroll content, so scrolling moves both
      // anchor and document natively, including compositor-only scroll frames.
      const x = rect.left - origin.left + root.scrollLeft - root.clientLeft + parseFloat(style.borderLeftWidth);
      const y = rect.top - origin.top + root.scrollTop - root.clientTop + parseFloat(style.borderTopWidth);
      const width = rect.width - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth);
      const height = rect.height - parseFloat(style.borderTopWidth) - parseFloat(style.borderBottomWidth);
      const visible = rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth;
      doc.priority = view.detail ? 100 : visible ? 20 : 0;
      if (!surface) continue;
      surface.hidden = width <= 0 || height <= 0 || doc.state !== "ready";
      Object.assign(surface.style, {
        left: `${x}px`, top: `${y}px`, width: `${width}px`, height: `${height}px`,
        borderRadius: style.borderRadius,
      });
    }
  }
}
