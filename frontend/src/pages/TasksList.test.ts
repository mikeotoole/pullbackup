import { afterEach, describe, expect, it, vi } from "vitest";
import { lastRunTime, relTime } from "../lib/relativeTime";

describe("task relative times", () => {
  afterEach(() => vi.useRealTimers());

  it("treats a timezone-less API timestamp as UTC", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-12T18:00:00Z"));

    expect(relTime("2026-08-12T17:55:00")).toBe("5 minutes ago");
  });

  it("keeps historical Last Run language elapsed despite clock skew", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-12T18:00:00Z"));

    expect(lastRunTime("2026-08-12T18:05:00Z")).toBe("0 seconds ago");
    expect(relTime("2026-08-12T18:05:00Z")).toBe("in 5 minutes");
  });

  it("does not disguise malformed timestamps", () => {
    expect(lastRunTime("not-a-timestamp")).toBe("Invalid date");
  });

  it("renders normal past and future boundaries", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-12T18:00:00Z"));

    expect(relTime("2026-08-12T17:59:30Z")).toBe("30 seconds ago");
    expect(relTime("2026-08-12T18:30:00Z")).toBe("in 30 minutes");
  });
});
