import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, LOGIN_PATH, UnauthenticatedError } from "./api";

// Behavioural tests for the auth-relevant parts of the API client.
//
// The first draft of these guards read api.ts as text and asserted that the
// strings "credentials", "401" and "/login" appeared somewhere in the file.
// Mutation testing showed that was worthless: deleting `credentials` from the
// real request helper still passed, because the same word survived elsewhere
// in the module. These drive the actual code instead.

const calls = [];
let assigned = [];

function respond(status, body = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

beforeEach(() => {
  calls.length = 0;
  assigned = [];
  globalThis.window = {
    location: {
      pathname: "/tasks",
      assign: (url) => assigned.push(url),
    },
  };
});

afterEach(() => {
  vi.restoreAllMocks();
  delete globalThis.window;
  delete globalThis.fetch;
});

function stubFetch(...responses) {
  let i = 0;
  globalThis.fetch = vi.fn(async (url, init) => {
    calls.push({ url, init });
    return responses[Math.min(i++, responses.length - 1)];
  });
}

describe("api auth behaviour", () => {
  it("sends the session cookie on ordinary api requests", async () => {
    stubFetch(respond(200, { version: "x" }));

    await api.systemInfo();

    // Without same-origin credentials the browser omits the cookie and every
    // call looks unauthenticated, which is a silent total auth failure.
    expect(calls[0].init.credentials).toBe("same-origin");
  });

  it("sends the session cookie on logout too", async () => {
    stubFetch(respond(200, { authenticated: false }));

    await api.logout();

    expect(calls[0].init.credentials).toBe("same-origin");
    expect(calls[0].init.method).toBe("POST");
  });

  it("sends credentials on login so the issued cookie is stored", async () => {
    stubFetch(respond(200, { authenticated: true }));

    await api.login("ci", "secret");

    expect(calls[0].url).toBe("/api/auth/login");
    expect(calls[0].init.credentials).toBe("same-origin");
    expect(JSON.parse(calls[0].init.body)).toEqual({
      username: "ci",
      password: "secret",
    });
  });

  it("redirects an unauthenticated api call to the login page", async () => {
    stubFetch(respond(401, { detail: "authentication required" }));

    await expect(api.listTasks()).rejects.toBeInstanceOf(UnauthenticatedError);

    // The operator lands on the login page instead of a raw 401 body.
    expect(assigned).toEqual([LOGIN_PATH]);
  });

  it("does not redirect away from the login page itself", async () => {
    // Otherwise a wrong password reloads the page and the error is never seen.
    globalThis.window.location.pathname = LOGIN_PATH;
    stubFetch(respond(401, { detail: "authentication required" }));

    await expect(api.session()).rejects.toBeInstanceOf(UnauthenticatedError);

    expect(assigned).toEqual([]);
  });

  it("reports a wrong password instead of redirecting", async () => {
    stubFetch(respond(401, { detail: "invalid credentials" }));

    await expect(api.login("ci", "wrong")).rejects.toThrow(/invalid username or password/);
    expect(assigned).toEqual([]);
  });

  it("explains a throttled login rather than showing a bare status code", async () => {
    stubFetch(respond(429, { detail: "too many failed sign-in attempts" }));

    await expect(api.login("ci", "wrong")).rejects.toThrow(/too many attempts/);
  });

  it("explains an unconfigured instance rather than showing a bare status code", async () => {
    stubFetch(respond(503, { detail: "authentication is not configured" }));

    await expect(api.login("ci", "x")).rejects.toThrow(/no credentials configured/);
  });
});
