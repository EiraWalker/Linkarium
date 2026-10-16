import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { errorMessage, request } from "../api/client";
import type { Envelope } from "../api/types";

export function useResource<T>(path: string | null, refresh = 0, poll = 0) {
  const [response, setResponse] = useState<Envelope<T> | null>(null);
  const [loadedPath, setLoadedPath] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const reload = useCallback(() => setRevision((r) => r + 1), []);
  const sequence = useRef(0);
  useEffect(() => {
    if (!path) {
      setResponse(null);
      setLoadedPath(null);
      return;
    }
    const controller = new AbortController();
    const seq = ++sequence.current;
    let timer: ReturnType<typeof setTimeout>;
    let finished = false;
    setError("");
    async function load() {
      if (!response || loadedPath !== path) setLoading(true);
      try {
        const result = await request<T>(path!, { signal: controller.signal });
        if (seq === sequence.current) {
          setResponse(result);
          setLoadedPath(path);
          setError("");
          const state =
            result.data &&
            typeof result.data === "object" &&
            "state" in result.data
              ? result.data.state
              : undefined;
          finished =
            typeof state === "string" &&
            [
              "succeeded",
              "failed",
              "cancelled",
              "completed",
              "partial",
            ].includes(state);
        }
      } catch (err) {
        if (!controller.signal.aborted && seq === sequence.current)
          setError(errorMessage(err));
      } finally {
        if (seq === sequence.current) setLoading(false);
        if (poll && !finished && !controller.signal.aborted)
          timer = setTimeout(
            load,
            document.hidden ? Math.max(poll, 15000) : poll,
          );
      }
    }
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [path, refresh, revision, poll]);
  return {
    data: loadedPath === path ? (response?.data ?? null) : null,
    page: loadedPath === path ? response?.page : undefined,
    loading: loading || (!!path && loadedPath !== path && !error),
    error,
    reload,
    setResponse,
  };
}
export function useRoute() {
  const [url, setUrl] = useState(() => new URL(location.href));
  useEffect(() => {
    const update = () => setUrl(new URL(location.href));
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  const navigate = useCallback((path: string, replace = false) => {
    const target = new URL(path, location.origin);
    if (target.origin !== location.origin) return;
    history[replace ? "replaceState" : "pushState"](
      {},
      "",
      target.pathname + target.search,
    );
    setUrl(target);
  }, []);
  const params = useCallback(
    (changes: Record<string, string | undefined>, replace = false) => {
      const next = new URL(location.href);
      for (const [key, value] of Object.entries(changes))
        value
          ? next.searchParams.set(key, value)
          : next.searchParams.delete(key);
      navigate(next.pathname + next.search, replace);
    },
    [navigate],
  );
  return { url, navigate, params };
}
export type Route = ReturnType<typeof useRoute>;
export function useMedia(query: string) {
  const [matches, set] = useState(() => matchMedia(query).matches);
  useEffect(() => {
    const media = matchMedia(query);
    const update = () => set(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);
  return matches;
}
