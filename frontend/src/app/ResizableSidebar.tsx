import type { ComponentChildren } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import s from "./App.module.css";

type Layout = "wide" | "compact" | "mobile";
type DesktopLayout = Exclude<Layout, "mobile">;
type Widths = Record<DesktopLayout, number>;
const minWidth = 208;
const maxWidth = 300;
const defaults: Widths = { wide: 250, compact: 208 };
const storageKey = (layout: DesktopLayout) => `website-sidebar-width-${layout}`;
const clamp = (width: number) => Math.max(minWidth, Math.min(maxWidth, Math.round(width)));

function currentLayout(): Layout {
  return window.matchMedia("(max-width: 767px)").matches ? "mobile"
    : window.matchMedia("(min-width: 1200px)").matches ? "wide" : "compact";
}

function savedWidths(): Widths {
  const widths = { ...defaults };
  for (const layout of ["wide", "compact"] as const) {
    try {
      const stored = localStorage.getItem(storageKey(layout));
      const value = stored === null ? null : JSON.parse(stored);
      if (typeof value === "number" && Number.isInteger(value) && value >= minWidth && value <= maxWidth) {
        widths[layout] = value;
      }
    } catch {}
  }
  return widths;
}

export function ResizableSidebar({ open, children }: { open: boolean; children: ComponentChildren }) {
  const [layout, setLayout] = useState<Layout>(currentLayout);
  const [widths, setWidths] = useState<Widths>(savedWidths);
  const [resizing, setResizing] = useState(false);
  const latestWidths = useRef(widths);
  const drag = useRef<{
    pointerId: number; startX: number; startWidth: number;
    layout: DesktopLayout; handle: HTMLDivElement;
  } | null>(null);

  function saveWidth(mode: DesktopLayout) {
    try { localStorage.setItem(storageKey(mode), JSON.stringify(latestWidths.current[mode])); } catch {}
  }
  function changeWidth(mode: DesktopLayout, width: number, persist = false) {
    const next = { ...latestWidths.current, [mode]: clamp(width) };
    latestWidths.current = next;
    setWidths(next);
    if (persist) saveWidth(mode);
  }
  function finishDrag(cancelled = false) {
    const active = drag.current;
    if (!active) return;
    drag.current = null;
    if (cancelled) changeWidth(active.layout, active.startWidth);
    else saveWidth(active.layout);
    if (active.handle.hasPointerCapture(active.pointerId)) active.handle.releasePointerCapture(active.pointerId);
    setResizing(false);
  }

  useEffect(() => {
    const wide = window.matchMedia("(min-width: 1200px)");
    const mobile = window.matchMedia("(max-width: 767px)");
    const update = () => {
      finishDrag(true);
      setLayout(currentLayout());
    };
    wide.addEventListener("change", update);
    mobile.addEventListener("change", update);
    return () => {
      wide.removeEventListener("change", update);
      mobile.removeEventListener("change", update);
      const active = drag.current;
      drag.current = null;
      if (active?.handle.hasPointerCapture(active.pointerId)) active.handle.releasePointerCapture(active.pointerId);
    };
  }, []);
  useEffect(() => {
    if (!resizing) return;
    const { cursor, userSelect } = document.body.style;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    return () => {
      document.body.style.cursor = cursor;
      document.body.style.userSelect = userSelect;
    };
  }, [resizing]);

  const width = layout === "mobile" ? defaults.compact : widths[layout];
  return (
    <>
      <aside id="collection-navigation" class={`${s.sidebar} ${open ? s.navOpen : ""}`}
        aria-label="收藏导航" style={{ "--sidebar-width": `${width}px` }}>
        {children}
      </aside>
      <div class={`${s.sidebarResize} ${resizing ? s.resizing : ""}`} role="separator"
        tabIndex={layout === "mobile" ? -1 : 0} aria-label="调整收藏导航宽度"
        aria-orientation="vertical" aria-controls="collection-navigation"
        aria-valuemin={minWidth} aria-valuemax={maxWidth} aria-valuenow={width}
        aria-valuetext={`${width}像素`}
        onPointerDown={e => {
          if (layout === "mobile" || e.button !== 0 || drag.current) return;
          e.preventDefault();
          e.currentTarget.focus();
          e.currentTarget.setPointerCapture(e.pointerId);
          drag.current = { pointerId: e.pointerId, startX: e.clientX,
            startWidth: latestWidths.current[layout], layout, handle: e.currentTarget };
          setResizing(true);
        }}
        onPointerMove={e => {
          const active = drag.current;
          if (active?.pointerId === e.pointerId) changeWidth(active.layout, active.startWidth + e.clientX - active.startX);
        }}
        onPointerUp={e => { if (drag.current?.pointerId === e.pointerId) finishDrag(); }}
        onPointerCancel={e => { if (drag.current?.pointerId === e.pointerId) finishDrag(true); }}
        onLostPointerCapture={() => finishDrag(true)}
        onKeyDown={e => {
          if (e.key === "Escape" && drag.current) {
            e.preventDefault();
            finishDrag(true);
          } else if (layout !== "mobile" && !drag.current && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
            e.preventDefault();
            changeWidth(layout, latestWidths.current[layout] + (e.key === "ArrowRight" ? 10 : -10), true);
          }
        }} />
    </>
  );
}
