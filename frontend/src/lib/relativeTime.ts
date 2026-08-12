type RelativeDirection = "both" | "past";

const HAS_TIMEZONE = /(?:Z|[+-]\d{2}:?\d{2})$/i;

export function relTime(iso: string | null, direction: RelativeDirection = "both") {
  if (!iso) return "N/A";
  // SQLite drops UTC tzinfo when reading DateTime columns, so Pydantic emits
  // timezone-less ISO strings for last_run_at. The database contract is UTC.
  const normalized = HAS_TIMEZONE.test(iso) ? iso : `${iso}Z`;
  const timestamp = new Date(normalized).getTime();
  if (!Number.isFinite(timestamp)) return "Invalid date";

  const rawSeconds = Math.round((timestamp - Date.now()) / 1000);
  // A completed run is historical by definition. Clamp server/client clock skew
  // to now instead of presenting Last Run with future-tense language.
  const seconds = direction === "past" ? Math.min(rawSeconds, 0) : rawSeconds;
  const abs = Math.abs(seconds);
  const fmt = (n: number, unit: string) => `${n} ${unit}${n !== 1 ? "s" : ""}`;
  const value = abs < 60
    ? fmt(abs, "second")
    : abs < 3600
      ? fmt(Math.round(abs / 60), "minute")
      : abs < 86400
        ? fmt(Math.round(abs / 3600), "hour")
        : fmt(Math.round(abs / 86400), "day");

  return seconds <= 0 ? `${value} ago` : `in ${value}`;
}

export const lastRunTime = (iso: string | null) => relTime(iso, "past");
