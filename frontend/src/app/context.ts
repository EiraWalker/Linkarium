import { createContext } from "preact";
import { useContext } from "preact/hooks";
import type { Folder, Settings, Tag, ImportBatch } from "../api/types";
export type ReviewScope = { importId?: string; bookmarkId?: string; groupId?: string };
import type { Route } from "./hooks";
export interface AppContext {
  folders: Folder[];
  tags: Tag[];
  settings: Settings | null;
  refresh: number;
  changed: () => void;
  notify: (message: string) => void;
  route: Route;
  trackGroup: (id: string) => void;
  review: (scope: ReviewScope) => void;
  imports: ImportBatch[];
  reloadImports: () => void;
  libraryIdentity: string;
}
export const AppState = createContext<AppContext | null>(null);
export function useApp() {
  const value = useContext(AppState);
  if (!value) throw new Error("Missing app context");
  return value;
}
export function flattenFolders(
  folders: Folder[],
  parent: string | null = null,
): Folder[] {
  return folders.flatMap((folder) => [
    { ...folder, parent_id: folder.parent_id ?? parent },
    ...flattenFolders(folder.children ?? [], folder.id),
  ]);
}
export function folderPath(
  folders: Folder[],
  id: string | null | undefined,
): string {
  const folder = folders.find((f) => f.id === id);
  if (!folder) return "未分类";
  const path: string[] = [];
  const seen = new Set<string>();
  let current: Folder | undefined = folder;
  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    path.unshift(current.name);
    current = folders.find((f) => f.id === current!.parent_id);
  }
  return path.join(" / ");
}
