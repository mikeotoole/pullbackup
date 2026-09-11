// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Mike O'Toole
import { SORT_COLUMNS, type SortColumn, type SortPreference } from "./taskSort";

/**
 * Where the chosen task ordering lives.
 *
 * Deliberately browser-local. The ordering is a viewing habit, not a property
 * of the deployment: someone triaging failures on their laptop wants "state,
 * descending" without imposing it on the operator watching next-run times on a
 * wall display, and nobody wants a table preference in the API's data model.
 * Hence localStorage, hence no user/account component in the key.
 */
export const SORT_STORAGE_KEY = "pullbackup.tasks.sort";

/** The subset of `Storage` used here. `null`/`undefined` means "no storage". */
type StorageLike = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
} | null | undefined;

/**
 * The browser's localStorage, or `null` where it is unavailable.
 *
 * Merely *reading* `window.localStorage` throws in a Chrome tab with cookies
 * blocked for the site, so even this access is guarded.
 */
export function browserStorage(): StorageLike {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

const isColumn = (v: unknown): v is SortColumn =>
  typeof v === "string" && (SORT_COLUMNS as readonly string[]).includes(v);

/**
 * Read the stored preference, or `null` for the default order.
 *
 * Everything that is not exactly a known column plus a known direction falls
 * back: absent, unparseable, wrong shape, a column that used to exist, a
 * direction from a future version. Storage is shared with whatever else the
 * browser profile has done and survives upgrades, so it is untrusted input.
 */
export function loadSort(storage: StorageLike = browserStorage()): SortPreference | null {
  let raw: string | null;
  try {
    raw = storage?.getItem(SORT_STORAGE_KEY) ?? null;
  } catch {
    return null;
  }
  if (!raw) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;

  const { column, direction } = parsed as { column?: unknown; direction?: unknown };
  if (!isColumn(column)) return null;
  if (direction !== "asc" && direction !== "desc") return null;
  return { column, direction };
}

/**
 * Persist the preference, or remove it when cleared.
 *
 * Never throws. A blocked or full localStorage costs the user a preference that
 * does not survive reload — it must not cost them the page.
 */
export function saveSort(pref: SortPreference | null, storage: StorageLike = browserStorage()): void {
  try {
    if (pref === null) storage?.removeItem(SORT_STORAGE_KEY);
    else storage?.setItem(SORT_STORAGE_KEY, JSON.stringify(pref));
  } catch {
    /* preference is best-effort; the table still works unsorted-by-default */
  }
}
