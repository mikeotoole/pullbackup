import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";

// The backend omits `WWW-Authenticate: Basic` from its 401 when it can tell the
// caller is a browser, so the native credential dialog never appears in front
// of our login page. `X-Pullbackup-Client: web` is the deterministic half of
// that signal — `Sec-Fetch-*` is absent on a plain-http deployment, so if this
// header goes missing the popup comes back on exactly the LAN installs that
// cannot use Fetch Metadata.
//
// Behavioural, not source-matching: what matters is the header the browser
// actually puts on the wire, whatever shape the call takes.

const ok = (body) =>
  Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(""),
  });

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(response = ok({})) {
  const fetchMock = vi.fn(() => response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const headersOf = (fetchMock) => new Headers(fetchMock.mock.calls[0][1].headers);

describe("api client identification", () => {
  it("marks a plain GET as coming from the web client", async () => {
    const fetchMock = stubFetch(ok([]));
    await api.listTasks();
    expect(headersOf(fetchMock).get("X-Pullbackup-Client")).toBe("web");
  });

  it("keeps the marker on a call that supplies its own init and headers", async () => {
    // A POST passes `method` and `body` through `init`; a naive spread of
    // `init` over the defaults drops the header for exactly these calls.
    const fetchMock = stubFetch(ok({}));
    await api.createTask({ name: "t" });
    const headers = headersOf(fetchMock);
    expect(headers.get("X-Pullbackup-Client")).toBe("web");
    // ...without losing the JSON content type the API needs.
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("marks the login request too", async () => {
    // Login bypasses the shared helper, so it needs the header on its own.
    const fetchMock = stubFetch(ok({}));
    await api.login("user", "pass");
    expect(headersOf(fetchMock).get("X-Pullbackup-Client")).toBe("web");
  });

  it("still sends the session cookie", async () => {
    // Regression guard: the header was added by rearranging this options
    // object, and `credentials` lives in the same object.
    const fetchMock = stubFetch(ok([]));
    await api.listTasks();
    expect(fetchMock.mock.calls[0][1].credentials).toBe("same-origin");
  });
});
