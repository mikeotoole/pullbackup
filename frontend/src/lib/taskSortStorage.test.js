import { afterEach, describe, expect, it, vi } from "vitest";

import { SORT_STORAGE_KEY, loadSort, saveSort } from "./taskSortStorage";

/** A minimal in-memory Storage. */
function memoryStorage(seed = {}) {
  const map = new Map(Object.entries(seed));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    _map: map,
  };
}

/** A Storage that throws on every access, as Safari private mode / blocked cookies do. */
const hostileStorage = {
  getItem() {
    throw new DOMException("denied", "SecurityError");
  },
  setItem() {
    throw new DOMException("denied", "QuotaExceededError");
  },
  removeItem() {
    throw new DOMException("denied", "SecurityError");
  },
};

afterEach(() => vi.restoreAllMocks());

describe("loading a persisted sort", () => {
  it("restores a valid stored preference", () => {
    const store = memoryStorage({
      [SORT_STORAGE_KEY]: JSON.stringify({ column: "last_run", direction: "desc" }),
    });
    expect(loadSort(store)).toEqual({ column: "last_run", direction: "desc" });
  });

  it("falls back to the default order when nothing is stored", () => {
    expect(loadSort(memoryStorage())).toBeNull();
  });

  it("falls back when the stored value is not JSON", () => {
    expect(loadSort(memoryStorage({ [SORT_STORAGE_KEY]: "{not json" }))).toBeNull();
  });

  it("falls back when the stored JSON is the wrong shape", () => {
    for (const stored of ["null", '"last_run"', "42", "[]", "{}", '{"column":"last_run"}']) {
      expect(loadSort(memoryStorage({ [SORT_STORAGE_KEY]: stored })), stored).toBeNull();
    }
  });

  it("falls back when the stored column is stale or unknown", () => {
    // A column that was removed from the table, or a value from a future
    // version, must not select an ordering that no longer exists.
    const store = memoryStorage({
      [SORT_STORAGE_KEY]: JSON.stringify({ column: "actions", direction: "asc" }),
    });
    expect(loadSort(store)).toBeNull();
  });

  it("falls back when the stored direction is not asc or desc", () => {
    const store = memoryStorage({
      [SORT_STORAGE_KEY]: JSON.stringify({ column: "last_run", direction: "sideways" }),
    });
    expect(loadSort(store)).toBeNull();
  });

  it("does not throw when storage is blocked", () => {
    expect(() => loadSort(hostileStorage)).not.toThrow();
    expect(loadSort(hostileStorage)).toBeNull();
  });

  it("does not throw when there is no storage at all", () => {
    expect(loadSort(null)).toBeNull();
    expect(loadSort(undefined)).toBeNull();
  });
});

describe("saving a sort", () => {
  it("writes the preference as JSON under the documented key", () => {
    const store = memoryStorage();
    saveSort({ column: "state", direction: "desc" }, store);
    expect(JSON.parse(store.getItem(SORT_STORAGE_KEY))).toEqual({
      column: "state",
      direction: "desc",
    });
  });

  it("round-trips through load", () => {
    const store = memoryStorage();
    saveSort({ column: "source", direction: "asc" }, store);
    expect(loadSort(store)).toEqual({ column: "source", direction: "asc" });
  });

  it("removes the key when the preference is cleared", () => {
    const store = memoryStorage({ [SORT_STORAGE_KEY]: '{"column":"state","direction":"asc"}' });
    saveSort(null, store);
    expect(store.getItem(SORT_STORAGE_KEY)).toBeNull();
  });

  it("does not throw when storage is blocked or full", () => {
    expect(() => saveSort({ column: "state", direction: "asc" }, hostileStorage)).not.toThrow();
    expect(() => saveSort(null, hostileStorage)).not.toThrow();
  });

  it("does not throw when there is no storage at all", () => {
    expect(() => saveSort({ column: "state", direction: "asc" }, null)).not.toThrow();
  });

  it("keys the preference to this browser profile only", () => {
    // The key must be a plain local name — no user id, no account, nothing that
    // implies the choice travels with a login.
    expect(SORT_STORAGE_KEY).toMatch(/^pullbackup\./);
    expect(SORT_STORAGE_KEY).not.toMatch(/user|account|session|token/i);
  });
});
